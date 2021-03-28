#!/usr/bin/env python3
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
parser.add_argument("-F", "--footer", metavar='footer.gcode', help="always send this file as a footer after a gcode transmit")
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

class Gcli:
	# "self" is too long for my weak fingers, s it shall be


	def __init__(s, args):
		s.args = args
		s.bootwait = args.bootwait / 1000
		s.prompt = '? '


	def cursor_refresh(s):
		cursx = len(s.prompt) + s.i_x
		if cursx >= curses.COLS:
			cursx = curses.COLS - 1

		s.iw.move(0, cursx)
		s.iw.refresh()


	def input_refresh(s):
		max_len = curses.COLS - len(s.prompt) - 1
		s.iw.addstr(0,0, s.prompt)
		s.iw.addstr(s.i_eh[s.i_y][:max_len])
		s.iw.clrtoeol()
		s.cursor_refresh()


	def full_refresh(s):
		s.dw.noutrefresh(0,0, 0,0, curses.LINES-2, curses.COLS - 1)
		s.input_refresh()


	def dw_refresh(s):
		s.dw.noutrefresh(0,0, 0,0, curses.LINES-2, curses.COLS - 1)
		s.cursor_refresh()


	def fn_complete(s, pfx):
		(head, tail) = os.path.split(pfx)
		if head == '':
			head = '.'

		gcodes = []
		dirs = []
		other = []
		try:
			with os.scandir(head) as it:
				for e in it:
					if e.name.startswith(tail):
						if e.is_file():
							if e.name.endswith(".gcode"):
								gcodes.append(e)
							else:
								other.append(e)
						elif e.is_dir():
							dirs.append(e)
						else:
							other.append(e)
		except OSError:
			pass
		for list in (gcodes, dirs, other):
			if len(list) == 0:
				continue

			if len(list) == 1:
				d = ''
				if list[0].is_dir():
					d = '/'
				return list[0].name[len(tail):] + d

			m = ''
			for e in list:
				if m:
					m += ' '
				m += e.name
				if e.is_dir():
					m += '/'

			s.huhmessage(m)
			pfx = os.path.commonprefix([e.name for e in list])
			return pfx[len(tail):]

		return ''


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

	# keyboard input
	def inputprocess(s):
		k = getukey(s.iw)
		if not k:
			return

		if k == curses.KEY_RESIZE:
			s.resize()
			return

		# This is used to pause/interrupt "stuff" (gcode transmit now) on any key
		# except resize, because that's not a key lol
		s.i_int = k

		if isinstance(k, int): # Special keys
			if k == curses.KEY_LEFT:
				if s.i_x:
					s.i_x -= 1
			elif k == curses.KEY_RIGHT:
				s.i_x += 1
				if s.i_x > len(s.i_eh[s.i_y]):
					s.i_x = len(s.i_eh[s.i_y])
			elif k == curses.KEY_DC:
				s.i_eh[s.i_y] = s.i_eh[s.i_y][:s.i_x] + s.i_eh[s.i_y][s.i_x+1:]
			elif k == curses.KEY_HOME:
				s.i_x = 0
			elif k == curses.KEY_END:
				s.i_x = len(s.i_eh[s.i_y])
			elif k == curses.KEY_UP:
				if s.i_y:
					s.i_y -= 1
					s.i_x = len(s.i_eh[s.i_y])
			elif k == curses.KEY_DOWN:
				if s.i_y < len(s.i_eh)-1:
					s.i_y += 1
					s.i_x = len(s.i_eh[s.i_y])

		else:
			if k == '\t' and s.i_x == len(s.i_eh[s.i_y]): # Tab filename completion
				cs = s.i_eh[s.i_y].split(maxsplit=1)
				if len(cs) == 2 and len(cs[1]) and cs[0][0].islower():
					s.i_eh[s.i_y] += s.fn_complete(cs[1])
					s.i_x = len(s.i_eh[s.i_y])
			elif k == '\n':
				if len(s.i_eh[s.i_y]):
					s.i_out = s.i_eh[s.i_y]
					if len(s.i_history) == 0 or s.i_history[len(s.i_history) - 1] != s.i_out:
						s.i_history.append(s.i_out)
					s.i_y = len(s.i_history)
					s.i_eh = s.i_history[:] + ['']
					s.i_x = 0
			elif k == chr(127) or k == chr(8):
				if s.i_x:
					s.i_eh[s.i_y] = s.i_eh[s.i_y][:s.i_x - 1] + s.i_eh[s.i_y][s.i_x:]
					s.i_x -= 1
			elif ord(k) >= 32:
				s.i_eh[s.i_y] = s.i_eh[s.i_y][:s.i_x] + k + s.i_eh[s.i_y][s.i_x:]
				s.i_x += 1

		s.input_refresh()


	def banner(s, str):
		s.dw.attron(s.banner_attr)
		s.dw.addstr('### ' + str + ' ###\n')
		s.dw.attroff(s.banner_attr)
		s.dw_refresh()


	def set_prompt(s, p):
		s.prompt = p
		s.input_refresh()


	def pause_gsender(s):
		s.gstate['paused'] = True
		s.banner('G-Code Transmit Paused')
		s.set_prompt('> ')


	def resume_gsender(s):
		s.gstate['paused'] = False
		s.gstate['waitok'] = False
		s.i_int = None
		s.set_prompt('! ')

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


	def waitio(s, timeout, doread = True):
		rv = False
		input = False
		(r,_,_) = select.select([s.ser,sys.stdin],[],[], timeout)
		for f in r:
			if f is s.ser:
				if doread:
					d = s.ser.read(4096)
					if len(d):
						s.bt = time.monotonic()
						s.outputprocess(d)
						rv = True
				else:
					rv = True
			if f is sys.stdin:
				input = True

		if input or (timeout and len(r) == 0):
			s.inputprocess()

		if len(s.recdata):
			t = time.monotonic() - s.bt
			if t > 1.0:
				flush_recdata()

		return rv


	def bootwaiter(s):
		rt = (s.bt + s.bootwait) - time.monotonic()
		if rt <= 0:
			return True

		if rt > 0.5:
			rt = 0.5

		s.waitio(rt)
		return False


	def send_line(s, l):
		l += '\n'
		s.ser.write(l.encode('utf-8'))
		s.dw.addstr('> ' + l)
		s.dw_refresh()


	def gcodesender(s):
		timeout = 0
		if s.gstate['waitok'] or s.gstate['paused']:
			timeout = 0.5

		s.waitio(timeout)

		if s.gstate['paused']:
			return False

		if s.i_int:
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
			if s.footer is None or s.gstate['gfile'] == s.footer:
				s.banner('Sent {} lines of G-Code in {:.3f} seconds'
						.format(s.gstate['line'], time.monotonic() - s.gstate['st']))
				return True
			else:
				s.infomessage('footer =')
				s.footer.seek(0,0)
				s.gstate['gfile'] = s.footer
				return False

		l = l.rsplit(sep=';',maxsplit=1)[0].rstrip()
		if len(l) == 0:
			return False

		s.send_line(l)
		s.gstate['waitok'] = True
		s.gstate['line'] += 1


	def start_gsender(s, gcode, flushint=True):
		s.banner('Sending G-Code')
		# gcodesender state
		s.gstate = { 'paused': False, 'waitok': False, 'gfile': gcode, 'line': 0, 'st': time.monotonic() }
		s.flush_recdata()
		s.set_prompt('! ')
		s.action = s.gcodesender
		if flushint:
			s.i_int = None


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


	cmds = (
		( ( 'q', 'quit', 'exit' ), "Quit. Duh." ),
		( ( 'c', 'cont', 'continue', 'resume' ), "Continue sending G-Code." ),
		( ( 're', 'resend' ), "Resend current g-code file from beginning." ),
		( ( 'f', 'file', 'send' ), "open and send a g-code file by filename." ),
		( ( 'setfooter', ), "Set g-code file to be used as a footer." ),
		( ( 'sf', 'sendfooter'), "Send (only) the footer file." ),
		( ( '?', 'h', 'help' ), "This thing..." )
		)


	def open_or_usage(s, cs, name):
		if len(cs) < 2:
			s.infomessage('usage: ' + cs[0] + ' ' + name)
			return False

		try:
			f = open(cs[1])
		except (FileNotFoundError, OSError):
			s.errmessage('Could not open "' + cs[1] + '"')
			return False

		return f


	def commandparser(s, cmd):
		cs = cmd.split(maxsplit=1)
		if cmd in s.cmds[0][0]: # quit
			return True
		elif cmd in s.cmds[1][0]: # continue
			if s.gstate:
				s.resume_gsender()
		elif cmd in s.cmds[2][0]: # resend
			if s.gcode:
				s.gcode.seek(0, 0)
				s.start_gsender(s.gcode)
			else:
				s.huhmessage('No gcode file to resend')
		elif cs[0] in s.cmds[3][0]: # file / send
			f = s.open_or_usage(cs, '<filename.gcode>')
			if f is False:
				return False

			if s.gcode:
				s.gcode.close()

			s.gcode = f
			s.start_gsender(s.gcode)
		elif cs[0] in s.cmds[4][0]: # footer
			f = s.open_or_usage(cs, '<footer.gcode>')
			if f is False:
				return False

			if s.footer:
				s.footer.close()

			s.footer = f
			s.huhmessage("footer: " + cs[1])
		elif cmd in s.cmds[5][0]: # send footer
			if s.footer is None:
				s.huhmessage("No footer to send (use setfooter)")
				return False

			s.footer.seek(0,0)
			s.start_gsender(s.footer)
		elif cmd in s.cmds[len(s.cmds)-1][0]: # help
			s.infomessage('Command list:')
			for c in s.cmds:
				str = ''
				for n in c[0]:
					if str:
						str += ' / '
					str += n
				str += ': '
				str += c[1]
				s.infomessage(str)
			s.infomessage('Capitalized commands are sent to the remote device.')
		else:
			s.huhmessage('Unknown command: ' + cmd)
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

		if s.args.gcode:
			s.gcode = open(s.args.gcode)
		else:
			s.gcode = None

		if s.args.footer:
			s.footer = open(s.args.footer)
		else:
			s.footer = None

		s.ser = serial.Serial(s.args.port, s.args.baud, timeout=0)

		# display window
		# it is a pad to avoid curses resizing it on us and losing the
		# latest (lowest) data when making a terminal smaller, other
		# than that we use it just like a window at 0,0.
		s.dw = curses.newpad(curses.LINES - 1, curses.COLS)
		s.dw.scrollok(True)

		# input window
		s.iw = curses.newwin(1, curses.COLS, curses.LINES - 1, 0)
		# Do not wait inside curses
		s.iw.nodelay(True)
		# need to enable keypad for this window, wrapper only does it for stdcsr
		s.iw.keypad(True)

		# gsender state (when running)
		s.gstate = None
		# Serial port Received Data buffer
		s.recdata = b''
		# Keyboard Input handler variables
		s.i_int = None
		s.i_out = None
		s.i_history = [] # permanent history
		s.i_eh = [''] # "Editable history", as in the current line editing context
		s.i_y = 0 # Eh "Y coordinate" (list position)
		s.i_x = 0 # Cursor position on the current eh line
		s.bt = time.monotonic()
		s.action = None

		if s.gcode:
			s.banner('Waiting for device boot')
			s.action = s.bootwaiter
		else:
			s.echo_attr |= curses.A_BOLD
			s.prompt = '> '

		# Display prompt
		s.input_refresh()

		while True: # main action loop
			if s.action:
				if s.action():
					if s.action == s.bootwaiter:
						s.start_gsender(s.gcode, False)
						s.echo_attr |= curses.A_BOLD
					else:
						s.action = None
						s.gstate = None
						s.set_prompt('> ')
			else:
				s.waitio(0.5)

			if s.i_out:
				os = s.i_out
				s.i_out = None
				if os[0].isupper():
					s.send_line(os)
				else:
					if s.commandparser(os):
						return


def main(scr, args):
	g = Gcli(args)
	g.run()

curses.wrapper(main, args)

