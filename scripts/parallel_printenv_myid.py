"""
Prototype code for parallel processing on a multi-core machine.
Companion code is rapid/scripts/printenv_myid.py
"""


import os
from concurrent.futures import ProcessPoolExecutor, as_completed

import modules.utils.rapid_pipeline_subs as util


rapid_sw = os.getenv('RAPID_SW')

if rapid_sw is None:

    print("*** Error: Env. var. RAPID_SW not set; quitting...")
    exit(64)

print("rapid_sw =",rapid_sw)


def run_script(myid):

    """
    Load unique value of MYID into the environment.
    Launch single instance of script with given setting for MYID.
    """


    os.environ['MYID'] = str(myid)

    python_cmd = 'python3'
    launch_single_instance_code = rapid_sw + '/scripts/printenv_myid.py'

    launch_cmd = [python_cmd,
                  launch_single_instance_code]

    exitcode_from_launch_cmd = util.execute_command(launch_cmd)


def launch_parallel_processes(myids, num_cores=None):

    if num_cores is None:
        num_cores = os.cpu_count()  # Use all available cores if not specified

    print("num_cores =",num_cores)

    with ProcessPoolExecutor(max_workers=num_cores) as executor:
        # Submit all tasks to the executor and store the futures in a list
        futures = [executor.submit(run_script,myid) for myid in myids]

        # Iterate over completed futures and update progress
        for i, future in enumerate(as_completed(futures)):
            index = futures.index(future)  # Find the original index/order of the completed future
            print(f"Completed: {i+1} processes, lastly for index={index}")


if __name__ == '__main__':

    myid_list = []
    for i in range(0,20):
        myid = i * 42
        myid_list.append(myid)
        print("i,myid =",i,myid)

    launch_parallel_processes(myid_list)
