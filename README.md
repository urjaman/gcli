## gcli.py

Hey, if you've got a better snazzy name, lmk.
Anyways this thing is a line-oriented serial port monitor and G-Code sender,
aimed for 3D printers or whatnot - my Anet A8 was the test target.
It knows to wait for the target to boot and stop chattering before sending
the G-Code file (and well, that does involve waiting for ok responses too.).

The main quirks to know are:
- commands sent to the target are Capitalized. Gxx/Mxxx naturally are.
- commands to the script/application are in lowercase.
- Any key (if recognized by curses) pauses G-code transmit.

The rest should be intuitive enough; -h for command-line 
parameters, "h" for runtime help.

Requirements:
- linux
- python3
- pyserial
	
Fancy features:
- basic line editing
- command history
- filename tab-completion (that likes to look for *.gcode)
- can send a fixed footer gcode file after every gcode file
- colors!
