#!/usr/bin/env python3
# See file named COPYING in the source distribution for license terms (MIT).

import serial
import os
import sys
import argparse
import time
import select
import curses

parser = argparse.ArgumentParser()
parser.add_argument("port", help="serial port device")
parser.add_argument("gcode", help="gcode file to transmit", nargs="?", default=None)
parser.add_argument("-b", "--baud", type=int, default=115200, help="serial port baudrate")
parser.add_argument("-P", "--parity", choices=["None", "Even", "Odd", "Mark", "Space"], default="None")
parser.add_argument("-X", "--xonxoff", action="store_const", const=True, default=False)
parser.add_argument("-S", "--stopbits", choices=["1", "1.5", "2"], default="1")
parser.add_argument("-w", "--bootwait", metavar="MS", type=int, default=4000, help="milliseconds to wait for boot messages")
parser.add_argument("-H", "--header", metavar="header.gcode", help="always send this file as a header before a gcode transmit")
parser.add_argument("-F", "--footer", metavar="footer.gcode", help="always send this file as a footer after a gcode transmit")
parser.add_argument("-E", "--emergency", metavar="emerg.gcode", help="send this if the Insert key is pressed (emergency stop)")
parser.add_argument("--scrollback", type=int, help="lines of scrollback to remember")
args = parser.parse_args()


def getukey(w):
    k = w.getch()
    if k == -1:
        return False

    if k > 255:
        return k

    if (k & 0xC0) == 0xC0:  # UTF-8 ?
        if (k & 0xE0) == 0xC0:
            l = 2
        elif (k & 0xF0) == 0xE0:
            l = 3
        elif (k & 0xF8) == 0xF0:
            l = 4
        else:
            # Not an UTF-8 first byte, just return the keycode
            return k

        bs = bytearray([k])
        while len(bs) < l:
            nk = w.getch()
            if nk == -1:  # Huh, the terminal didnt get the byte to us yet?
                time.sleep(0.01)
                continue
            bs.append(nk)

        return bs.decode("utf-8")

    return chr(k)


class InputMethod:
    def __init__(self, window, prompt, file_suffix, completion_msg, commands, scroll=None, resize=None, emergency=None):
        self.iw = window
        self.prompt = prompt
        self.file_suffix = file_suffix
        self.msg = completion_msg
        self.commands = commands
        self.scroll = scroll if scroll else lambda x: None
        self.resize = resize
        self.emergency = emergency

        # edit context ( a full editable copy of history + current line )
        self.e = [""]
        self.x = 0
        self.y = 0
        # command history
        self.history = []
        # visual X: current cursor position
        self.visx = 0

        # Do not wait inside curses
        self.iw.nodelay(True)
        # need to enable keypad for this window, wrapper only does it for stdcsr
        self.iw.keypad(True)

        # outputs: "Interrupt" (last key storage), and the output commandline
        self.intr = None
        self.out = None

    def cursor_refresh(self):
        self.iw.move(0, self.visx)
        self.iw.refresh()

    def redraw(self):
        visx = None
        try:
            self.iw.addstr(0, 0, self.prompt)
            self.iw.addstr(self.e[self.y][: self.x])
            (_, visx) = self.iw.getyx()
            self.iw.addstr(self.e[self.y][self.x :])
        except curses.error:
            if visx is None:
                (_, visx) = self.iw.getyx()
        self.visx = visx
        self.iw.clrtoeol()
        self.cursor_refresh()

    def set_prompt(self, newprompt):
        self.prompt = newprompt
        self.redraw()

    def choose_complete(self, prefix, list):
        list = [e for e in list if e.startswith(prefix)]
        if len(list) == 0:
            return ""

        if len(list) == 1:
            return list[0][len(prefix) :]

        self.msg(" ".join(list))
        pfx = os.path.commonprefix(list)
        return pfx[len(prefix) :]

    def fn_complete(self, pfx):
        (head, tail) = os.path.split(pfx)
        head = head if head else "."

        special = []
        dirs = []
        other = []
        try:
            with os.scandir(head) as it:
                for e in it:
                    if e.name.startswith(tail):
                        if e.is_file():
                            if e.name.endswith(self.file_suffix):
                                special.append(e.name)
                            else:
                                other.append(e.name)
                        elif e.is_dir():
                            dirs.append(e.name + os.path.sep)
                        else:
                            other.append(e.name)
        except OSError:
            pass

        if tail == "..":
            dirs.append(tail + os.path.sep)

        for list in (special, dirs, other):
            if len(list) == 0:
                continue

            return self.choose_complete(tail, list)

        return ""

    # keyboard input
    def process(self):
        k = getukey(self.iw)
        if not k:
            return

        if k == curses.KEY_RESIZE:
            if self.resize:
                self.resize()
            return

        # This is used to pause/interrupt "stuff" (gcode transmit now) on any key
        # except resize, because that's not a key lol
        self.intr = k

        e, x, y = self.e, self.x, self.y

        if isinstance(k, int) and k == curses.KEY_BACKSPACE:
            k = chr(8)

        if isinstance(k, int):  # Special keys
            if k == curses.KEY_IC and self.emergency:
                self.emergency()
                return
            elif k == curses.KEY_LEFT:
                if x:
                    x -= 1
            elif k == curses.KEY_RIGHT:
                x += 1
                if x > len(e[y]):
                    x = len(e[y])
            elif k == curses.KEY_DC:
                e[y] = e[y][:x] + e[y][x + 1 :]
            elif k == curses.KEY_HOME:
                x = 0
            elif k == curses.KEY_END:
                x = len(e[y])
            elif k == curses.KEY_UP:
                if y:
                    y -= 1
                    x = len(e[y])
            elif k == curses.KEY_DOWN:
                if y < len(e) - 1:
                    y += 1
                    x = len(e[y])
            elif k == curses.KEY_SR:
                self.scroll(-1)
            elif k == curses.KEY_SF:
                self.scroll(1)
            elif k == curses.KEY_PPAGE:
                self.scroll(-10)
            elif k == curses.KEY_NPAGE:
                self.scroll(10)
        else:
            if k == "\t" and x == len(e[y]):  # Tab completion (filenames, commands)
                if " " in e[y]:
                    cs = e[y].split(maxsplit=1)
                    if len(cs) == 2 and len(cs[1]) and cs[0][0].islower():
                        e[y] += self.fn_complete(cs[1])
                else:
                    e[y] += self.choose_complete(e[y], self.commands)

                x = len(e[y])
            elif k == "\n":
                if len(e[y]):
                    self.out = e[y]
                    if len(self.history) == 0 or self.history[-1] != self.out:
                        self.history.append(self.out)
                    y = len(self.history)
                    e = self.history[:] + [""]
                    x = 0
            elif k == chr(127) or k == chr(8):
                if x:
                    e[y] = e[y][: x - 1] + e[y][x:]
                    x -= 1
            elif ord(k) >= 32:
                e[y] = e[y][:x] + k + e[y][x:]
                x += 1

        self.e, self.x, self.y = e, x, y
        self.redraw()

    def output(self):
        r = self.out
        self.out = None
        return r


class DisplayBox:
    def __init__(self, w, h, scrollback, refresh):
        self.w = w
        self.refresh = refresh
        self.lines = [[]]
        self.scrollback = scrollback

        self.heights(h)
        self.p = curses.newpad(self.padh, w)
        self.p.scrollok(True)

        self.yoff = 0
        self.ymax = 0

    def heights(self, h):
        self.h = h
        self.padh = self.scrollback + h
        self.lines_keep = self.padh + 25
        self.lines_max = self.padh + 100

    def refreshbox(self, y, x):
        self.p.noutrefresh(self.yoff, 0, y, x, y + self.h - 1, x + self.w - 1)

    def ymath(self):
        (y, _) = self.p.getyx()
        ym = y - (self.h - 1)
        if ym < 0:
            ym = 0
        self.ymax = ym
        self.yoff = ym

    def redraw(self):
        self.p.move(0, 0)
        self.p.erase()
        for l in self.lines[-self.padh :]:
            for str, attr in l:
                self.p.attron(attr)
                self.p.addstr(str)
                self.p.attroff(attr)
        self.p.redrawwin()
        self.ymath()

    def scroll(self, lines):
        self.yoff += lines
        self.yoff = self.yoff if self.yoff >= 0 else 0
        self.yoff = self.yoff if self.yoff <= self.ymax else self.ymax
        self.refresh()

    def resize(self, w, h):
        self.w = w
        self.heights(h)
        self.p.resize(self.padh, w)
        self.redraw()

    # print(str, [attr=0], [str, attr], ...)
    def print(self, *args):
        for i in range(0, len(args), 2):
            str = args[i]
            attr = args[i + 1] if (i + 1) < len(args) else 0

            self.lines[-1].append((str, attr))
            if str[-1] == "\n":
                self.lines.append([])
                if len(self.lines) >= self.lines_max:
                    self.lines = self.lines[-self.lines_keep :]
            self.p.attron(attr)
            self.p.addstr(str)
            self.p.attroff(attr)

        self.ymath()
        self.refresh()


class GCodeFile:
    def __init__(self, filename, identity, next=None, cl=False):
        self.identity = identity
        self.next = next
        self.autoclose = cl
        self.f = open(filename) if filename else None

    def __bool__(self):
        return bool(self.f)

    def open(self, fn):
        try:
            nf = open(fn)
        except OSError:
            return False
        if self.f:
            self.f.close()
        self.f = nf
        return True

    def reset(self):
        self.f.seek(0, 0)

    def readline(self):
        l = self.f.readline()
        if self.autoclose and l == "":
            self.f.close()
            self.f = None
        return l


class Gcli:
    def __init__(self, args):
        self.args = args
        self.bootwait = args.bootwait / 1000
        if args.scrollback is None:
            meminfo = dict((i.split()[0].rstrip(":"), int(i.split()[1])) for i in open("/proc/meminfo").readlines())
            mem_kib = meminfo["MemTotal"]
            if mem_kib > 500000:  # Hardware on which scrollback memory use doesnt really matter
                self.scrollback = 10000
            elif mem_kib > 20000:  # Smallish
                self.scrollback = 1000
            else:  # Tiny AF.
                self.scrollback = 100
        else:
            self.scrollback = args.scrollback

    def disp_refresh(self):
        self.d.refreshbox(0, 0)
        self.i.cursor_refresh()

    def resize(self):
        curses.update_lines_cols()
        self.iw.mvwin(curses.LINES - 1, 0)
        self.iw.resize(1, curses.COLS)
        self.d.resize(curses.COLS, curses.LINES - 1)
        self.iw.redrawwin()
        self.d.refreshbox(0, 0)
        self.i.redraw()

    def banner(self, str):
        self.d.print("### " + str + " ###\n", self.banner_attr)

    def huhmessage(self, str):
        self.d.print("? " + str + "\n", self.huh_attr)

    def errmessage(self, str):
        self.d.print("! " + str + "\n", self.error_attr)

    def infomessage(self, str):
        self.d.print("= " + str + "\n", self.info_attr)

    def send_emergency(self):
        if not self.emergency:
            self.huhmessage("No emergency gcode file to send")
            return
        self.start_gsender(self.emergency, msg="Sending Emergency G-Code")
        self.gcodesender()  # send first line NOW

    def pause_gsender(self):
        self.gstate["paused"] = True
        self.banner("G-Code Transmit Paused")
        self.i.set_prompt("> ")

    def resume_gsender(self):
        if self.gstate is None:
            return
        self.gstate["paused"] = False
        self.gstate["waitok"] = False
        self.i.intr = None
        self.i.set_prompt("! ")

    # serial input, display output
    def outputprocess(self, data):
        self.recdata = self.recdata + data
        while b"\n" in self.recdata:
            p = self.recdata.split(sep=b"\n", maxsplit=1)
            if len(p) == 1:
                p[1] = b""

            self.recdata = p[1]
            output = p[0].strip()
            outstr = output.decode("utf-8", errors="ignore")
            out_attr = self.echo_attr
            if output == b"ok":
                if self.gstate and self.gstate["waitok"]:
                    self.gstate["waitok"] = False
                    continue
                out_attr = self.ok_attr

            if output.startswith(b"error"):
                out_attr = self.error_attr

            self.d.print("< " + outstr + "\n", out_attr)
            if self.gstate and output.startswith(b"error"):
                self.pause_gsender()

    def flush_recdata(self):
        if len(self.recdata):
            d = self.recdata.decode("utf-8", errors="ignore")
            self.d.print("< " + d, self.echo_attr, "|\n", self.error_attr)
            self.recdata = b""

    def waitio(self, timeout):
        (r, _, _) = select.select([self.ser, sys.stdin], [], [], timeout)
        if self.ser in r:
            d = self.ser.read(4096)
            if len(d):
                self.last_receive = time.monotonic()
                self.outputprocess(d)

        if sys.stdin in r or len(r) == 0:
            self.i.process()

        if len(self.recdata):
            t = time.monotonic() - self.last_receive
            if t > 1.0:
                flush_recdata()

    def bootwaiter(self):
        rt = (self.last_receive + self.bootwait) - time.monotonic()
        if rt <= 0:
            return True

        if rt > 0.5:
            rt = 0.5

        self.select_to = rt
        return False

    def send_line(self, l):
        l += "\n"
        self.ser.write(l.encode("utf-8"))
        self.d.print("> " + l)

    def gcodesender(self):
        if self.gstate["paused"]:
            return False

        if self.i.intr:
            self.pause_gsender()
            return False

        if self.gstate["waitok"]:
            return False

        while True:
            try:
                l = self.gstate["gfile"].readline()
            except ValueError:
                self.banner("Binary data in G-Code File - Aborting Transmit")
                return True

            if l == "":
                self.gstate["gfile"] = self.gstate["gfile"].next
                if self.gstate["gfile"]:
                    self.infomessage(self.gstate["gfile"].identity + " =")
                    self.gstate["gfile"].reset()
                    continue
                else:
                    self.banner(
                        "Sent {} lines of G-Code in {:.3f} seconds".format(
                            self.gstate["line"], time.monotonic() - self.gstate["st"]
                        )
                    )
                    return True

            l = l.rsplit(sep=";", maxsplit=1)[0].rstrip()
            if len(l) == 0:
                continue

            self.send_line(l)
            self.gstate["waitok"] = True
            self.gstate["line"] += 1
            return False

    def start_gsender(self, gcode, flushint=True, msg=None):
        if not gcode:
            self.huhmessage("No " + gcode.identity + " file to (re)send")
            return

        # Automatically substitute self.header for self.gcode if provided
        if self.header and self.header.next is gcode:
            gcode = self.header

        if msg is None:
            msg = "Sending G-Code: " + gcode.identity

        self.banner(msg)
        # gcodesender state
        self.gstate = {"paused": False, "waitok": False, "gfile": gcode, "line": 0, "st": time.monotonic()}
        gcode.reset()
        self.flush_recdata()
        self.i.set_prompt("! ")
        self.action = self.gcodesender
        if flushint:
            self.i.intr = None

    class Cmd:
        list = []  # intentionally shared list of commands

        def __init__(self, names, func, h, params=0):
            self.names = names
            self.help = h
            self.run = func
            self.params = params
            self.list.append(self)

    def cmd_open(self, f, cs, name, send=False):
        if len(cs) < 2:
            self.infomessage("usage: " + cs[0] + " " + name)
            return

        if f.open(cs[1]):
            self.infomessage(f.identity + ": " + cs[1])
            if send:
                self.start_gsender(f)
        else:
            self.errmessage('Could not open "' + cs[1] + '"')

    def cmd_help(self):
        self.infomessage("Command list:")
        for c in self.Cmd.list:
            self.infomessage(" / ".join(c.names) + ": " + c.help)

        self.infomessage("Capitalized commands are sent to the remote device.")

    Cmd(("q", "quit"), lambda self: True, "Quit. Duh.")
    Cmd(("c", "continue"), resume_gsender, "Continue sending G-Code.")
    Cmd(("re", "resend"), lambda self: self.start_gsender(self.gcode), "Resend current g-code file from beginning.")
    Cmd(
        ("f", "file", "send"),
        lambda s, cs: self.cmd_open(self.gcode, cs, "<filename.gcode>", True),
        params=1,
        h="open and send a g-code file by filename.",
    )
    Cmd(("e",), send_emergency, h="send the emergency g-code")
    Cmd(
        ("setemergency",),
        lambda s, cs: self.cmd_open(self.emergency, cs, "<emergency.gcode>"),
        params=1,
        h="Set g-code file for emergency stop (Insert key or 'e' command)",
    )
    Cmd(
        ("setheader",),
        lambda s, cs: self.cmd_open(self.header, cs, "<header.gcode>"),
        params=1,
        h="Set g-code file to be used as a header.",
    )
    Cmd(
        ("setfooter",),
        lambda s, cs: self.cmd_open(self.footer, cs, "<footer.gcode>"),
        params=1,
        h="Set g-code file to be used as a footer.",
    )
    Cmd(("sf", "sendfooter"), lambda self: self.start_gsender(self.footer), "Send (only) the footer file.")
    Cmd(
        ("once",),
        lambda s, cs: self.cmd_open(self.sendonce, cs, "<once.gcode>", True),
        params=1,
        h="send a gcode file by filename once - no header or footer.",
    )
    Cmd(("?", "h", "help"), cmd_help, "This thing...")

    def commandparser(self, cmd):
        cs = cmd.split(maxsplit=1)
        for c in self.Cmd.list:
            if cs[0] in c.names:
                if c.params:
                    return c.run(self, cs)
                if len(cs) > 1:
                    self.huhmessage(cs[0] + " takes no parameters")
                    return
                return c.run(self)

        self.huhmessage("Unknown command: " + cs[0])
        return False

    def run(self):
        if curses.has_colors():
            curses.use_default_colors()
            curses.init_pair(1, curses.COLOR_YELLOW, -1)
            curses.init_pair(2, curses.COLOR_BLUE, -1)
            curses.init_pair(3, curses.COLOR_RED, -1)
            curses.init_pair(4, curses.COLOR_CYAN, -1)
            curses.init_pair(5, curses.COLOR_MAGENTA, -1)

            self.banner_attr = curses.A_BOLD | curses.color_pair(1)
            self.ok_attr = curses.A_BOLD | curses.color_pair(2)
            self.error_attr = curses.A_BOLD | curses.color_pair(3)
            self.echo_attr = curses.color_pair(4)
            self.huh_attr = curses.A_BOLD | curses.color_pair(5)
            self.info_attr = curses.color_pair(4)
        else:
            self.banner_attr = curses.A_STANDOUT
            self.ok_attr = 0
            self.error_attr = curses.A_STANDOUT
            self.echo_attr = 0
            self.huh_attr = 0
            self.info_attr = 0

        partext = "N"
        parity = serial.PARITY_NONE
        if self.args.parity == "Even":
            parity = serial.PARITY_EVEN
            partext = "E"
        elif self.args.parity == "Odd":
            parity = serial.PARITY_ODD
            partext = "O"
        elif self.args.parity == "Mark":
            parity = serial.PARITY_MARK
            partext = "M"
        elif self.args.parity == "Space":
            parity = serial.PARITY_SPACE
            partext = "S"

        stoptxt = "1"
        stopbits = serial.STOPBITS_ONE
        if self.args.stopbits == "1.5":
            stopbits = serial.STOPBITS_ONE_POINT_FIVE
            stoptxt = "1p5"
        elif self.args.stopbits == "2":
            stopbits = serial.STOPBITS_TWO
            stoptxt = "2"

        # Open things (files, serial)
        self.footer = GCodeFile(self.args.footer, "footer")
        self.gcode = GCodeFile(self.args.gcode, "gcode", self.footer)
        self.header = GCodeFile(self.args.header, "header", self.gcode)
        self.emergency = GCodeFile(self.args.emergency, "emergency")
        self.ser = serial.Serial(
            self.args.port, self.args.baud, parity=parity, stopbits=stopbits, xonxoff=self.args.xonxoff, timeout=0
        )
        # a GCodeFile object for the once command (no file yet)
        self.sendonce = GCodeFile(None, "sendonce", cl=True)

        # display window/pad class
        self.d = DisplayBox(curses.COLS, curses.LINES - 1, self.scrollback, self.disp_refresh)

        # input window and the input class to (mostly) handle it
        self.iw = curses.newwin(1, curses.COLS, curses.LINES - 1, 0)
        # list of command-names that we tabcomplete
        commands = [c.names[-1] + c.params * " " for c in self.Cmd.list]
        self.i = InputMethod(self.iw, "? ", ".gcode", self.huhmessage, commands, self.d.scroll, self.resize, self.send_emergency)

        # gsender state (when running)
        self.gstate = None
        # Serial port Received Data buffer
        self.recdata = b""

        self.last_receive = time.monotonic()
        self.action = None
        self.select_to = default_select_to = 0.5

        self.banner(
            f"Opened port {self.args.port} @ {self.args.baud} baud, {partext} parity, {stoptxt} stop bits, XonXoff:{str(self.args.xonxoff)}"
        )

        if self.gcode:
            self.banner("Waiting for device boot")
            self.action = self.bootwaiter
        else:
            self.echo_attr |= curses.A_BOLD
            self.i.set_prompt("> ")

        # Display prompt
        self.i.redraw()

        while True:  # main action loop
            if self.action:
                if self.action():
                    self.select_to = default_select_to
                    if self.action == self.bootwaiter:
                        self.start_gsender(self.gcode, False)
                        self.echo_attr |= curses.A_BOLD
                        continue
                    else:
                        self.action = None
                        self.gstate = None
                        self.i.set_prompt("> ")

            self.waitio(self.select_to)
            os = self.i.output()
            if os:
                if os[0].isupper():
                    self.send_line(os)
                else:
                    if self.commandparser(os):
                        return


def main(scr, args):
    g = Gcli(args)
    g.run()


curses.wrapper(main, args)
