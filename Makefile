all:
	echo "Use make style to run black"

style:
	black -l 130 gcli.py
