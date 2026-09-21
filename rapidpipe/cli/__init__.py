"""The command-line tool.

Holds ``rapidpipe``'s command-line entrypoint (``main.py``): dispatch to
``rapidpipe stage <name>`` and, in future, run creation, promotion and
deletion. This subpackage may import any other subpackage; nothing in the
package imports ``rapidpipe.cli`` back.
"""
