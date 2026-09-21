"""The terminal plane — channel B of §3.4.

``tmux pipe-pane`` taps a pane's raw bytes into a FIFO; a reader publishes them
on the bus; a writer appends them to rotating flat files and indexes where they
landed. The bytes never enter the database (§4.1 tier 2) — only
``{path, byte_offset, length}`` does.

Why the raw bytes at all, when the hook plane already reports structured state:
§2.5. Hooks cannot give the stream an operator expects to *see*, and the pipe
cannot say which tool is running without fragile output parsing. Together the
canvas is event-driven and the terminal view is byte-for-byte authentic.
"""
