"""
Run pipeline/ingestL2Files.py in an open loop, once every N seconds.

ingestL2Files.py is a one-shot: it works out what has not been ingested, ingests
it, and exits.  This daemon is what turns that into a standing service, so that
ASDF files arriving in the input bucket are picked up without anyone having to
start a run by hand.

Each cycle runs the ingest to completion, so two ingests can never overlap and
fight over the same work list.  The interval is measured from the START of one
cycle to the start of the next, not from the end of one to the start of the
next, so the cadence is the interval rather than the interval plus however long
the ingest happened to take.  A cycle that outlasts the interval -- a first run
over a full bucket certainly will -- is simply followed immediately by the next
one; the daemon says so in the log rather than trying to catch up.

The ingest child inherits this process's stdout and stderr, so its output
streams into the same log as the daemon's own, in order.  Redirect the daemon to
get a log:

    python3 pipeline/ingestL2FilesDaemon.py 300 >& ingestL2FilesDaemon.log &


Stopping it
-----------

SIGINT (control-C), SIGTERM and SIGQUIT are trapped.  The daemon finishes the
ingest that is running and then exits, rather than leaving a file half converted
or half registered.  Send the signal a second time to give up on that and let
the default handler kill the process immediately.

A control-C from a terminal reaches the ingest child as well, since it shares
the process group; the daemon notices the child died on a signal and stops
rather than starting another cycle.


Exit codes
----------

Failures are distinguished rather than lumped into one code, so that a
supervisor's log says which kind of failure stopped the daemon without anyone
having to open the ingest log to find out.  The values follow the BSD sysexits
convention RAPID already uses.

 0   Stopped cleanly: by a signal, or on INGESTL2FILESMAXCYCLES.
64   Bad or missing configuration: RAPID_SW not set, or an interval that is not
     a non-negative integer.
66   The ingest script is not where RAPID_SW says it is.
69   Another daemon already holds the lock.
70   Gave up on an ingest that kept failing; read its log for why.
73   The lock file could not be opened or created.

The daemon announces the code on the way out as

    terminating_exitcode = <code>

at every one of its exits, as ingestL2Files.py does, so a log can be grepped for
how a run ended.


Usage
-----

    python3 pipeline/ingestL2FilesDaemon.py [interval_seconds]

The interval may be given as the first command-line argument or as
INGESTL2FILESINTERVAL; the argument wins.  Everything ingestL2Files.py itself
needs (buckets, database, work directory) is read from the environment by that
script, and this one neither reads nor second-guesses it -- it only passes the
environment through.

RAPID_SW                        Root of the RAPID software tree, used to locate
                                pipeline/ingestL2Files.py and to set PYTHONPATH
                                for it.  Required.
INGESTL2FILESINTERVAL           Seconds from the start of one cycle to the start
                                of the next.  Defaults to 300, and must be at
                                least 1.
RAPIDPYTHON                     Python interpreter to run the ingest with.
                                Defaults to the one running this daemon.
INGESTL2FILESMAXCYCLES          Stop after this many cycles, for short tests.
                                Defaults to no limit.
INGESTL2FILESMAXFAILURES        Stop after this many consecutive failed cycles.
                                Defaults to 10; set to 0 to keep trying forever.
INGESTL2FILESLOCKFILE           Lock file that keeps two daemons from running
                                against the same buckets.  Defaults to
                                ingestL2FilesDaemon.lock under RAPID_WORK, or
                                under the current directory if RAPID_WORK is not
                                set.
"""

import os
import sys
import time
import fcntl
import signal
import subprocess
from datetime import datetime, timezone
from dateutil import tz

to_zone = tz.gettz('America/Los_Angeles')


# Define code name and version.

swname = "ingestL2FilesDaemon.py"
swvers = "1.0"

print("swname =", swname)
print("swvers =", swvers)


# Compute start time for benchmark.

start_time_benchmark = time.time()


# Compute processing datetime (UT) and processing datetime (Pacific time).

datetime_utc_now = datetime.utcnow()
proc_utc_datetime = datetime_utc_now.strftime('%Y-%m-%dT%H:%M:%SZ')
datetime_pt_now = datetime_utc_now.replace(tzinfo=timezone.utc).astimezone(tz=to_zone)
proc_pt_datetime_started = datetime_pt_now.strftime('%Y-%m-%dT%H:%M:%S PT')

print("proc_utc_datetime =",proc_utc_datetime)
print("proc_pt_datetime_started =",proc_pt_datetime_started)


# The name of the script this daemon exists to run, relative to RAPID_SW.

ingest_script_relative_path = "pipeline/ingestL2Files.py"


# Exit codes, following the BSD sysexits values that RAPID already uses: 64 for a bad or
# missing configuration, 66 for a required input that is not there, 69 for a service that is
# unavailable, 70 for a software failure, 73 for something that could not be created.  They
# are distinguished rather than lumped into one so that a supervisor's log says WHICH kind of
# failure stopped the daemon without anyone having to open the ingest log to find out.
#
# Every one of them is non-zero, which matters most for exit_code_ingest_failing: a daemon
# that gives up must not look like a clean shutdown, or whatever supervises it will leave it
# stopped.

exit_code_config = 64             # Bad or missing configuration: RAPID_SW unset, interval not a number.
exit_code_no_input = 66           # The ingest script is not where RAPID_SW says it is.
exit_code_already_running = 69    # Another daemon holds the lock.
exit_code_ingest_failing = 70     # Gave up on an ingest that kept failing.
exit_code_cannot_create = 73      # The lock file could not be opened or created.


#-------------------------------------------------------------------------------------------------------------
# Stopping.
#-------------------------------------------------------------------------------------------------------------

# Set to False by the signal handler, and read at the two points in the loop where stopping
# leaves nothing half done: after a cycle finishes, and during the wait between cycles.

keep_running = True


# True while an ingest is actually running, so that the message a signal prints says what will
# happen rather than assuming there is a cycle in flight to wait for.

ingest_running = False


def catch_zap(signal_number,frame):

    '''
    Ask the daemon to stop once the running ingest has finished.

    The default handler is put back immediately, so a second signal kills the process
    outright.  Someone who signals twice has decided not to wait for the ingest, and the
    daemon should not be the thing standing in their way.
    '''

    global keep_running

    keep_running = False

    signal.signal(signal_number,signal.SIG_DFL)

    if ingest_running:
        print(f"\n{timestamp()} Signal {signal_name(signal_number)} received; "
              "will stop after the running ingest finishes")
        print(f"{timestamp()} Signal again to stop immediately")
    else:
        print(f"\n{timestamp()} Signal {signal_name(signal_number)} received; "
              "no ingest is running, so stopping now")

    sys.stdout.flush()


def signal_name(signal_number):

    '''
    Return the name of a signal, or its number as a string when it has none.  A real-time
    signal has no member in signal.Signals, and signal.Signals(n) raises ValueError on one --
    which would be an exception thrown from the code whose only job is to report that
    something else went wrong.
    '''

    try:
        return signal.Signals(signal_number).name
    except ValueError:
        return f"signal {signal_number}"


def timestamp():

    '''
    Return the current Pacific time, for stamping the daemon's own log lines.  The ingest it
    runs stamps its own output, so these lines are the ones that say what the DAEMON did.
    '''

    return datetime.now(tz=to_zone).strftime('%Y-%m-%dT%H:%M:%S PT')


#-------------------------------------------------------------------------------------------------------------
# Methods.
#-------------------------------------------------------------------------------------------------------------

def acquire_lock(lock_filename):

    '''
    Take an exclusive lock, and return (file object, None) so that the lock stays held for as
    long as this process lives.  Returns (None, exit code) if it could not be taken, the code
    saying whether the file itself was the problem or another daemon already holds it.

    Two daemons against the same buckets would each build a work list, and the files on both
    lists would be converted, uploaded and registered twice -- the second registration making
    a needless second version of every one of them.  The lock is what stops a second daemon
    being started by accident.
    '''

    # Opened for appending rather than writing, so that a second daemon losing the race does
    # not truncate the pid of the one that holds the lock on its way to finding out it lost.

    try:
        fh = open(lock_filename,'a+',encoding="utf-8")
    except OSError as e:
        print(f"*** Error: Could not open lock file {lock_filename} ({e}); quitting...")
        return None,exit_code_cannot_create

    try:
        fcntl.flock(fh.fileno(),fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print(f"*** Error: Another {swname} holds {lock_filename}; quitting...")
        fh.close()
        return None,exit_code_already_running

    fh.seek(0)
    fh.truncate()
    fh.write(f"{os.getpid()}\n")
    fh.flush()

    return fh,None


def run_ingest(python_executable,ingest_script):

    '''
    Run one ingest to completion.  Returns (ok, exit_code, killed_by_signal).

    The child inherits stdout and stderr rather than having them captured, so that a run over
    thousands of files reports progress as it goes instead of in one lump at the end.
    '''

    print(f"{timestamp()} Executing {python_executable} {ingest_script}")

    sys.stdout.flush()

    try:
        completed = subprocess.run([python_executable,ingest_script])
    except OSError as e:
        print(f"*** Error: Could not execute {ingest_script} ({e})")
        return False,None,False

    returncode = completed.returncode


    # A negative returncode means the child was terminated by a signal, whose number is its
    # negation.  That is what a control-C in a terminal looks like from here, the child
    # sharing this process's group, so it is reported as a signal rather than as a failure of
    # the ingest itself.

    if returncode < 0:

        signal_number = -returncode

        print(f"*** Warning: {ingest_script} was terminated by "
              f"{signal_name(signal_number)}")

        return False,None,True

    if returncode != 0:
        print(f"*** Error: {ingest_script} exited with code {returncode}")
        return False,returncode,False

    return True,returncode,False


def wait_for_next_cycle(seconds):

    '''
    Wait out the remainder of the interval, returning early if the daemon has been asked to
    stop.  The wait is broken into one-second naps for exactly that reason: a stop signal
    arriving at the start of a five-minute interval should not be answered five minutes later.
    '''

    if seconds <= 0:
        return

    print(f"{timestamp()} Sleeping {seconds:.1f} seconds until the next cycle...")

    sys.stdout.flush()

    deadline = time.time() + seconds

    while keep_running:

        remaining = deadline - time.time()

        if remaining <= 0:
            break

        time.sleep(min(1.0,remaining))


def get_int_from_env(name,default,minimum=0):

    '''
    Return an integer environment variable, or the default if it is unset.  A value that is
    not a number, or is below the minimum, is an error rather than something to quietly
    default: a daemon started with a misspelled interval should say so and not run for days at
    the wrong cadence.

    The minimum is per variable because zero means different things to different ones.  For
    the cycle and failure limits it means "no limit" and is allowed; for the interval it would
    mean no wait at all, which is not a cadence but a spin -- with a trivial ingest that is
    eighty-odd cycles a second, each one listing both S3 buckets and querying the database.
    '''

    value_str = os.getenv(name)

    if value_str is None:
        return default

    try:
        value = int(value_str)
    except ValueError:
        print(f"*** Error: Env. var. {name} = {value_str} is not an integer; quitting...")
        print("terminating_exitcode =",exit_code_config)
        exit(exit_code_config)

    if value < minimum:
        print(f"*** Error: Env. var. {name} = {value} is less than {minimum}; quitting...")
        print("terminating_exitcode =",exit_code_config)
        exit(exit_code_config)

    return value


#-------------------------------------------------------------------------------------------------------------
# Main program.
#-------------------------------------------------------------------------------------------------------------

if __name__ == '__main__':


    # Unbuffered output, so the daemon's lines and the ingest's lines reach the log in the
    # order they happened, rather than the daemon's sitting in a buffer while the ingest runs.

    try:
        sys.stdout.reconfigure(line_buffering=True)
        sys.stderr.reconfigure(line_buffering=True)
    except AttributeError:
        pass


    # Trap the signals a daemon is stopped with.

    signal.signal(signal.SIGINT,catch_zap)
    signal.signal(signal.SIGTERM,catch_zap)
    signal.signal(signal.SIGQUIT,catch_zap)


    # Locate the ingest script.

    rapid_sw = os.getenv('RAPID_SW')

    if rapid_sw is None:
        print("*** Error: Env. var. RAPID_SW not set; quitting...")
        print("terminating_exitcode =",exit_code_config)
        exit(exit_code_config)

    ingest_script = os.path.join(rapid_sw,ingest_script_relative_path)

    if not os.path.exists(ingest_script):
        print(f"*** Error: {ingest_script} does not exist; quitting...")
        print("terminating_exitcode =",exit_code_no_input)
        exit(exit_code_no_input)


    # The ingest imports modules and database from the root of the software tree, so that root
    # has to be on its PYTHONPATH.  It is set in the Docker image; setting it here too is what
    # lets the daemon run from a plain git clone as well.

    pythonpath = os.getenv('PYTHONPATH')

    if pythonpath is None:
        os.environ['PYTHONPATH'] = rapid_sw
    elif rapid_sw not in pythonpath.split(os.pathsep):
        os.environ['PYTHONPATH'] = rapid_sw + os.pathsep + pythonpath


    # The interval, from the command line if given and from the environment otherwise.

    interval_seconds = get_int_from_env('INGESTL2FILESINTERVAL',300,minimum=1)

    if len(sys.argv) > 1:

        try:
            interval_seconds = int(sys.argv[1])
        except ValueError:
            print(f"*** Error: Interval {sys.argv[1]} is not an integer; quitting...")
            print(f"Usage: python3 {swname} [interval_seconds]")
            print("terminating_exitcode =",exit_code_config)
            exit(exit_code_config)

        if interval_seconds < 1:
            print(f"*** Error: Interval {interval_seconds} is less than 1 second; quitting...")
            print("terminating_exitcode =",exit_code_config)
            exit(exit_code_config)


    # Stop after this many cycles, or this many consecutive failures.  A daemon that keeps
    # failing is usually misconfigured rather than unlucky, and spinning on that forever just
    # fills the log; a limit turns it into something an operator will notice.  Transient
    # trouble -- the database being restarted, say -- is survived well inside the default.

    # Zero means "no limit" for both of these, so zero is allowed where it is not for the
    # interval above.

    max_cycles = get_int_from_env('INGESTL2FILESMAXCYCLES',0,minimum=0)
    max_failures = get_int_from_env('INGESTL2FILESMAXFAILURES',10,minimum=0)


    # The interpreter to run the ingest with.  Defaulting to this one means the daemon and the
    # ingest cannot end up in different Python environments.

    python_executable = os.getenv('RAPIDPYTHON')

    if python_executable is None:
        python_executable = sys.executable


    # The lock file.

    lock_filename = os.getenv('INGESTL2FILESLOCKFILE')

    if lock_filename is None:

        rapid_work = os.getenv('RAPID_WORK')

        if rapid_work is None:
            rapid_work = os.getcwd()

        lock_filename = os.path.join(rapid_work,"ingestL2FilesDaemon.lock")

    print("rapid_sw =",rapid_sw)
    print("ingest_script =",ingest_script)
    print("python_executable =",python_executable)
    print("interval_seconds =",interval_seconds)
    print("max_cycles =",max_cycles)
    print("max_failures =",max_failures)
    print("lock_filename =",lock_filename)
    print("pid =",os.getpid())

    lock_fh,lock_exit_code = acquire_lock(lock_filename)

    if lock_fh is None:
        print("terminating_exitcode =",lock_exit_code)
        exit(lock_exit_code)


    # Begin open loop.

    n_cycles = 0
    n_succeeded = 0
    n_failed = 0
    n_consecutive_failures = 0

    last_ingest_exit_code = None

    total_cycle_seconds = 0.0
    max_cycle_seconds = 0.0

    stop_reason = "signal"

    print(f"{timestamp()} Starting the ingest loop, one cycle every {interval_seconds} seconds")

    while keep_running:

        cycle_start_time = time.time()

        n_cycles += 1

        print("")
        print(f"{timestamp()} ===== Cycle {n_cycles} =====")

        ingest_running = True

        ok,ingest_exit_code,killed_by_signal = run_ingest(python_executable,ingest_script)

        ingest_running = False

        if ingest_exit_code is not None and ingest_exit_code != 0:
            last_ingest_exit_code = ingest_exit_code

        cycle_seconds = time.time() - cycle_start_time

        total_cycle_seconds += cycle_seconds

        if cycle_seconds > max_cycle_seconds:
            max_cycle_seconds = cycle_seconds

        if ok:
            n_succeeded += 1
            n_consecutive_failures = 0
        else:
            n_failed += 1
            n_consecutive_failures += 1

        print(f"{timestamp()} Cycle {n_cycles} finished in {cycle_seconds:.1f} seconds; "
              f"cycles so far: succeeded = {n_succeeded}, failed = {n_failed}")


        # A child killed by a signal means the signal was aimed at the whole process group,
        # which is what a control-C from a terminal does.  Take it as meant for the daemon too.

        if killed_by_signal:
            keep_running = False

        if not keep_running:
            break

        if max_failures > 0 and n_consecutive_failures >= max_failures:

            # The ingest's own exit code says what kind of failure it kept hitting -- 64 for a
            # misconfiguration, 67 for the database -- which is the first thing anyone reading
            # this will want.  The daemon still exits 70 itself, so that what supervises it
            # sees one code for "gave up" rather than whatever the ingest happened to return.

            print(f"*** Error: {n_consecutive_failures} consecutive failed cycles"
                  + (f", the last exiting {last_ingest_exit_code}"
                     if last_ingest_exit_code is not None else "")
                  + "; quitting...")

            stop_reason = "consecutive failures"
            break

        if max_cycles > 0 and n_cycles >= max_cycles:
            print(f"{timestamp()} Reached INGESTL2FILESMAXCYCLES = {max_cycles}; stopping...")
            stop_reason = "cycle limit"
            break


        # Wait out the rest of the interval, measured from the start of this cycle.

        seconds_until_next_cycle = interval_seconds - (time.time() - cycle_start_time)

        if seconds_until_next_cycle <= 0:
            print(f"{timestamp()} Cycle took longer than the {interval_seconds}-second "
                  "interval; starting the next one now...")
        else:
            wait_for_next_cycle(seconds_until_next_cycle)

    # End of open loop.


    if keep_running is False and stop_reason == "signal":
        print(f"\n{timestamp()} Stopped by signal")


    # Release the lock.  Closing the file drops the flock with it.
    #
    # The file itself is deliberately left in place.  flock applies to the inode, not to the
    # name, so removing it would let a daemon that opened the path just before the removal
    # keep a lock on an inode with no name, while the next daemon to start creates a fresh
    # inode and locks that instead -- two daemons, each satisfied it holds the lock, running
    # against the same buckets.  The file holds one pid and is truncated on reopen, so leaving
    # it costs nothing.

    lock_fh.close()


    # Code-timing benchmark.

    end_time_benchmark = time.time()

    print("")
    print("====================================================")
    print(f"{timestamp()} {swname} stopping ({stop_reason})")
    print("Cycles run =",n_cycles)
    print("Cycles succeeded =",n_succeeded)
    print("Cycles failed =",n_failed)

    if n_cycles > 0:
        print(f"Cycle time in seconds: total = {total_cycle_seconds:.1f}, "
              f"mean = {total_cycle_seconds / n_cycles:.1f}, max = {max_cycle_seconds:.1f}")

    print("Elapsed time in seconds for the daemon =",
        end_time_benchmark - start_time_benchmark)


    # Termination.  Stopping on a signal, or on the cycle limit, is a clean shutdown and exits
    # 0; giving up on an ingest that kept failing is a failure and exits with the same code
    # every other failure here does.

    if stop_reason == "consecutive failures":
        print("terminating_exitcode =",exit_code_ingest_failing)
        exit(exit_code_ingest_failing)

    print("terminating_exitcode =",0)

    exit(0)
