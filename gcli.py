#!/usr/bin/env python3
# See file named COPYING in the source distribution for license terms (MIT).

import serial
import os
import sys
import argparse
import struct
import time
import select
import curses

parser = argparse.ArgumentParser()
parser.add_argument("port", help="serial port device")
parser.add_argument("gcode", help="gcode file to transmit", nargs='?', default=None)
parser.add_argument("-b", "--baud", type=int, default=115200, help="serial port baudrate")
parser.add_argument("-w", "--bootwait", metavar='MS',type=int, default=4000, help="milliseconds to wait for boot messages")
parser.add_argument("-H", "--header", metavar='header.gcode', help="always send this file as a header before a gcode transmit")
parser.add_argument("-F", "--footer", metavar='footer.gcode', help="always send this file as a footer after a gcode transmit")
parser.add_argument("-E", "--emergency", metavar='emerg.gcode', help="send this if the Insert key is pressed (emergency stop)")
args = parser.parse_args()

def getukey(w):
	k = w.getch()
	if k == -1:
		return False

	if k > 255:
		return k

	if (k & 0xC0) == 0xC0: # UTF-8 ?
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
			if nk == -1: # Huh, the terminal didnt get the byte to us yet?
				time.sleep(0.01)
				continue
			bs.append(nk)

		return bs.decode('utf-8')

	return chr(k)


class GCodeFile:
	# i use "s" for self

	def __init__(s, filename, identity, next=None, cl=False):
		s.identity = identity
		s.next = next
		s.autoclose = cl;
		s.f = open(filename) if filename else None

	def __bool__(s):
		return True if s.f else False

	def open(s, fn):
		try:
			nf = open(fn)
		except OSError:
			return False
		if s.f:
			s.f.close()
		s.f = nf
		return True

	def reset(s):
		s.f.seek(0,0)

	def readline(s):
		l = s.f.readline()
		if s.autoclose and l == '':
			s.f.close()
			s.f = None
		return l


class InputMethod:
	def __init__(s, window, prompt, file_suffix, completion_msg, commands, resize=None, emergency=None):
		s.iw = window
		s.prompt = prompt
		s.file_suffix = file_suffix
		s.msg = completion_msg
		s.commands = commands
		s.resize = resize
		s.emergency = emergency

		# edit context ( a full editable copy of history + current line )
		s.e = ['']
		s.x = 0
		s.y = 0
		# command history
		s.history = []

		# Do not wait inside curses
		s.iw.nodelay(True)
		# need to enable keypad for this window, wrapper only does it for stdcsr
		s.iw.keypad(True)

		# outputs: "Interrupt" (last key storage), and the output commandline
		s.intr = None
		s.out = None


	def cursor_refresh(s):
		(_, cols) = s.iw.getmaxyx()
		cursx = len(s.prompt) + s.x
		if cursx >= cols:
			cursx = cols - 1

		s.iw.move(0, cursx)
		s.iw.refresh()


	def redraw(s):
		(_, cols) = s.iw.getmaxyx()
		max_len = cols - len(s.prompt) - 1
		s.iw.addstr(0,0, s.prompt)
		s.iw.addstr(s.e[s.y][:max_len])
		s.iw.clrtoeol()
		s.cursor_refresh()


	def set_prompt(s, newprompt):
		s.prompt = newprompt
		s.redraw()


	def choose_complete(s, prefix, list):
		list = [e for e in list if e.startswith(prefix)]
		if len(list) == 0:
			return ''

		if len(list) == 1:
			return list[0][len(prefix):]

		s.msg(' '.join(list))
		pfx = os.path.commonprefix(list)
		return pfx[len(prefix):]


	def fn_complete(s, pfx):
		(head, tail) = os.path.split(pfx)
		head = head if head else '.'

		special = []
		dirs = []
		other = []
		try:
			with os.scandir(head) as it:
				for e in it:
					if e.name.startswith(tail):
						if e.is_file():
							if e.name.endswith(s.file_suffix):
								special.append(e.name)
							else:
								other.append(e.name)
						elif e.is_dir():
							dirs.append(e.name + os.path.sep)
						else:
							other.append(e.name)
		except OSError:
			pass

		if tail == '..':
			dirs.append('../')

		for list in (special, dirs, other):
			if len(list) == 0:
				continue

			return s.choose_complete(tail, list)

		return ''


	# keyboard input
	def process(s):
		k = getukey(s.iw)
		if not k:
			return

		if k == curses.KEY_RESIZE:
			if s.resize:
				s.resize()
			return

		# This is used to pause/interrupt "stuff" (gcode transmit now) on any key
		# except resize, because that's not a key lol
		s.intr = k

		e, x, y = s.e, s.x, s.y

		if isinstance(k, int): # Special keys
			if k == curses.KEY_IC and s.emergency:
				s.emergency()
				return
			elif k == curses.KEY_LEFT:
				if x:
					x -= 1
			elif k == curses.KEY_RIGHT:
				x += 1
				if x > len(e[y]):
					x = len(e[y])
			elif k == curses.KEY_DC:
				e[y] = e[y][:x] + e[y][x+1:]
			elif k == curses.KEY_HOME:
				x = 0
			elif k == curses.KEY_END:
				x = len(e[y])
			elif k == curses.KEY_UP:
				if y:
					y -= 1
					x = len(e[y])
			elif k == curses.KEY_DOWN:
				if y < len(e)-1:
					y += 1
					x = len(e[y])

		else:
			if k == '\t' and x == len(e[y]): # Tab completion (filenames, commands)
				if ' ' in e[y]:
					cs = e[y].split(maxsplit=1)
					if len(cs) == 2 and len(cs[1]) and cs[0][0].islower():
						e[y] += s.fn_complete(cs[1])
				else:
					e[y] += s.choose_complete(e[y], s.commands)

				x = len(e[y])
			elif k == '\n':
				if len(e[y]):
					s.out = e[y]
					if len(s.history) == 0 or s.history[-1] != s.out:
						s.history.append(s.out)
					y = len(s.history)
					e = s.history[:] + ['']
					x = 0
			elif k == chr(127) or k == chr(8):
				if x:
					e[y] = e[y][:x-1] + e[y][x:]
					x -= 1
			elif ord(k) >= 32:
				e[y] = e[y][:x] + k + e[y][x:]
				x += 1

		s.e, s.x, s.y = e, x, y
		s.redraw()


	def output(s):
		r = s.out
		s.out = None
		return r


class Gcli:
	def __init__(s, args):
		s.args = args
		s.bootwait = args.bootwait / 1000


	def full_refresh(s):
		s.dw.noutrefresh(0,0, 0,0, curses.LINES-2, curses.COLS - 1)
		s.i.redraw()


	def dw_refresh(s):
		s.dw.noutrefresh(0,0, 0,0, curses.LINES-2, curses.COLS - 1)
		s.i.cursor_refresh()


	def resize(s):
		curses.update_lines_cols()
		(dw_y,_) = s.dw.getyx()
		max_ypos = curses.LINES - 2
		if dw_y > max_ypos:
			s.dw.scroll(dw_y - max_ypos)
			s.dw.move(max_ypos, 0)
		s.dw.resize(curses.LINES - 1, curses.COLS)
		s.iw.mvwin(curses.LINES - 1, 0)
		s.iw.resize(1, curses.COLS)
		s.dw.redrawwin()
		s.iw.redrawwin()
		s.full_refresh()


	def send_emergency(s):
		if not s.emergency:
			s.huhmessage('No emergency gcode file to send')
			return
		s.start_gsender(s.emergency, msg='Sending Emergency G-Code')
		s.gcodesender() # send first line NOW


	def banner(s, str):
		s.dw.attron(s.banner_attr)
		s.dw.addstr('### ' + str + ' ###\n')
		s.dw.attroff(s.banner_attr)
		s.dw_refresh()


	def pause_gsender(s):
		s.gstate['paused'] = True
		s.banner('G-Code Transmit Paused')
		s.i.set_prompt('> ')


	def resume_gsender(s):
		if s.gstate is None:
			return
		s.gstate['paused'] = False
		s.gstate['waitok'] = False
		s.i.intr = None
		s.i.set_prompt('! ')

	# serial input, display output
	def outputprocess(s, data):
		s.recdata = s.recdata + data
		while b'\n' in s.recdata:
			p = s.recdata.split(sep=b'\n', maxsplit=1)
			if len(p) == 1:
				p[1] = b''

			s.recdata = p[1]
			output = p[0].strip()
			outstr = output.decode('utf-8',errors='ignore')
			out_attr = s.echo_attr
			if output == b'ok':
				if s.gstate and s.gstate['waitok']:
					s.gstate['waitok'] = False
					continue
				out_attr = s.ok_attr

			if output.startswith(b'error'):
				out_attr = s.error_attr

			s.dw.attron(out_attr)
			s.dw.addstr('< ' + outstr + '\n')
			s.dw.attroff(out_attr)
			if s.gstate and output.startswith(b'error'):
				s.pause_gsender()

		s.dw_refresh()


	def flush_recdata(s):
		if len(s.recdata):
			d = s.recdata.decode('utf-8',errors='ignore')
			s.dw.attron(s.echo_attr)
			s.dw.addstr('< ' + d)
			s.dw.attroff(s.echo_attr)
			s.dw.attron(s.error_attr)
			s.dw.addstr('|\n')
			s.dw.attroff(s.error_attr)
			s.recdata = b''


	def waitio(s, timeout):
		input = False
		(r,_,_) = select.select([s.ser,sys.stdin],[],[], timeout)
		for f in r:
			if f is s.ser:
				d = s.ser.read(4096)
				if len(d):
					s.last_receive = time.monotonic()
					s.outputprocess(d)

			if f is sys.stdin:
				input = True

		if input or (timeout and len(r) == 0):
			s.i.process()

		if len(s.recdata):
			t = time.monotonic() - s.last_receive
			if t > 1.0:
				flush_recdata()


	def bootwaiter(s):
		rt = (s.last_receive + s.bootwait) - time.monotonic()
		if rt <= 0:
			return True

		if rt > 0.5:
			rt = 0.5

		s.select_to = rt
		return False


	def send_line(s, l):
		l += '\n'
		s.ser.write(l.encode('utf-8'))
		s.dw.addstr('> ' + l)
		s.dw_refresh()


	def gcodesender(s):
		if s.gstate['paused']:
			return False

		if s.i.intr:
			s.pause_gsender()
			return False

		if s.gstate['waitok']:
			return False

		try:
			l = s.gstate['gfile'].readline()
		except ValueError:
			s.banner('Binary data in G-Code File - Aborting Transmit')
			return True

		if l == '':
			s.gstate['gfile'] = s.gstate['gfile'].next
			if s.gstate['gfile']:
				s.infomessage(s.gstate['gfile'].identity + ' =')
				s.gstate['gfile'].reset()
				return False
			else:
				s.banner('Sent {} lines of G-Code in {:.3f} seconds'
						.format(s.gstate['line'], time.monotonic() - s.gstate['st']))
				return True

		l = l.rsplit(sep=';',maxsplit=1)[0].rstrip()
		if len(l) == 0:
			return False

		s.send_line(l)
		s.gstate['waitok'] = True
		s.gstate['line'] += 1


	def start_gsender(s, gcode, flushint=True, msg='Sending G-Code'):
		if not gcode:
			s.huhmessage('No ' + gcode.identity + ' file to (re)send')
			return

		# Automatically substitute s.header for s.gcode if provided
		if s.header and s.header.next is gcode:
			gcode = s.header

		s.banner(msg)
		# gcodesender state
		gcode.reset()
		s.gstate = { 'paused': False, 'waitok': False, 'gfile': gcode, 'line': 0, 'st': time.monotonic() }
		s.flush_recdata()
		s.i.set_prompt('! ')
		s.action = s.gcodesender
		if flushint:
			s.i.intr = None


	def message(s, str, pfx, attr):
		s.dw.attron(attr)
		s.dw.addstr(pfx + str + '\n')
		s.dw.attroff(attr)
		s.dw_refresh()


	def huhmessage(s, str):
		s.message(str, '? ', s.huh_attr)


	def errmessage(s, str):
		s.message(str, '! ', s.error_attr)


	def infomessage(s, str):
		s.message(str, '= ', s.info_attr)


	class Cmd:
		list = [] # intentionally shared list of commands
		def __init__(s, names, func, h, params=0):
			s.names = names
			s.help = h
			s.run = func
			s.params = params
			s.list.append(s)


	def cmd_open(s, f, cs, name, send=False):
		if len(cs) < 2:
			s.infomessage('usage: ' + cs[0] + ' ' + name)
			return

		if f.open(cs[1]):
			s.infomessage(f.identity + ': ' + cs[1])
			if send:
				s.start_gsender(f)
		else:
			s.errmessage('Could not open "' + cs[1] + '"')


	def cmd_help(s):
		s.infomessage('Command list:')
		for c in s.Cmd.list:
			str = ''
			for n in c.names:
				if str:
					str += ' / '
				str += n
			str += ': '
			str += c.help
			s.infomessage(str)
		s.infomessage('Capitalized commands are sent to the remote device.')


	Cmd(( 'q', 'quit' ), lambda s: True, "Quit. Duh." )
	Cmd(( 'c', 'continue' ), resume_gsender, "Continue sending G-Code." )
	Cmd(( 're', 'resend' ), lambda s: s.start_gsender(s.gcode), "Resend current g-code file from beginning." )
	Cmd(( 'f', 'file', 'send' ), lambda s, cs: s.cmd_open(s.gcode,  cs, '<filename.gcode>',True), params=1,
		h="open and send a g-code file by filename." )
	Cmd(( 'e', ), send_emergency, h="send the emergency g-code")
	Cmd(( 'setemergency', ), lambda s, cs: s.cmd_open(s.emergency, cs, '<emergency.gcode>'), params=1,
		h="Set g-code file for emergency stop (Insert key or 'e' command)" )
	Cmd(( 'setheader', ), lambda s, cs: s.cmd_open(s.header, cs, '<header.gcode>'), params=1,
		h="Set g-code file to be used as a header." )
	Cmd(( 'setfooter', ), lambda s, cs: s.cmd_open(s.footer, cs, '<footer.gcode>'), params=1,
		h="Set g-code file to be used as a footer." )
	Cmd(( 'sf', 'sendfooter'), lambda s: s.start_gsender(s.footer),
		"Send (only) the footer file." )
	Cmd(( 'once', ), lambda s, cs: s.cmd_open(s.sendonce, cs, '<once.gcode>', True), params=1,
		h="send a gcode file by filename once - no header or footer.")
	Cmd(( '?', 'h', 'help' ), cmd_help,  "This thing..." )


	def commandparser(s, cmd):
		cs = cmd.split(maxsplit=1)
		for c in s.Cmd.list:
			if cs[0] in c.names:
				if c.params:
					return c.run(s, cs)
				if len(cs) > 1:
					s.huhmessage(cs[0] + ' takes no parameters')
					return
				return c.run(s)

		s.huhmessage('Unknown command: ' + cs[0])
		return False


	def run(s):
		if curses.has_colors():
			curses.use_default_colors()
			curses.init_pair(1, curses.COLOR_YELLOW, -1)
			curses.init_pair(2, curses.COLOR_BLUE, -1)
			curses.init_pair(3, curses.COLOR_RED, -1)
			curses.init_pair(4, curses.COLOR_CYAN, -1)
			curses.init_pair(5, curses.COLOR_MAGENTA, -1)

			s.banner_attr = curses.A_BOLD | curses.color_pair(1)
			s.ok_attr = curses.A_BOLD | curses.color_pair(2)
			s.error_attr = curses.A_BOLD | curses.color_pair(3)
			s.echo_attr = curses.color_pair(4)
			s.huh_attr = curses.A_BOLD | curses.color_pair(5)
			s.info_attr = curses.color_pair(4)
		else:
			s.banner_attr = curses.A_STANDOUT
			s.ok_attr = 0
			s.error_attr = curses.A_STANDOUT
			s.echo_attr = 0
			s.huh_attr = 0
			s.info_attr = 0

		# Open things (files, serial)
		s.footer = GCodeFile(s.args.footer, 'footer')
		s.gcode = GCodeFile(s.args.gcode, 'gcode', s.footer)
		s.header = GCodeFile(s.args.header, 'header', s.gcode)
		s.emergency = GCodeFile(s.args.emergency, 'emergency')
		s.ser = serial.Serial(s.args.port, s.args.baud, timeout=0)

		s.sendonce = GCodeFile(None, 'sendonce', cl=True)

		# display window
		# it is a pad to avoid curses resizing it on us and losing the
		# latest (lowest) data when making a terminal smaller, other
		# than that we use it just like a window at 0,0.
		s.dw = curses.newpad(curses.LINES - 1, curses.COLS)
		s.dw.scrollok(True)

		# input window and the input class to (mostly) handle it

		s.iw = curses.newwin(1, curses.COLS, curses.LINES - 1, 0)
		# quick, make a list of valid commands
		commands = []
		for c in s.Cmd.list:
			for n in c.names:
				if c.params:
					commands.append(n + ' ')
				else:
					commands.append(n)
		commands = [c for c in commands if len(c) > 2]
		s.i = InputMethod(s.iw, '? ', '.gcode', s.huhmessage, commands, s.resize, s.send_emergency)

		# gsender state (when running)
		s.gstate = None
		# Serial port Received Data buffer
		s.recdata = b''

		s.last_receive = time.monotonic()
		s.action = None
		s.select_to = default_select_to = 0.5

		if s.gcode:
			s.banner('Waiting for device boot')
			s.action = s.bootwaiter
		else:
			s.echo_attr |= curses.A_BOLD
			s.i.set_prompt('> ')

		# Display prompt
		s.i.redraw()

		while True: # main action loop
			if s.action:
				if s.action():
					s.select_to = default_select_to
					if s.action == s.bootwaiter:
						s.start_gsender(s.gcode, False)
						s.echo_attr |= curses.A_BOLD
					else:
						s.action = None
						s.gstate = None
						s.i.set_prompt('> ')

			s.waitio(s.select_to)
			os = s.i.output()
			if os:
				if os[0].isupper():
					s.send_line(os)
				else:
					if s.commandparser(os):
						return


def main(scr, args):
	g = Gcli(args)
	g.run()

curses.wrapper(main, args)

