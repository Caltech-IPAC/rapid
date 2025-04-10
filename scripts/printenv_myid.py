"""
Prototype code for parallel processing on a multi-core machine.
Companion code is rapid/scripts/parallel_printenv_myid.py
"""

import os
import random
import time

myid = os.getenv('MYID')

if myid is None:

    print("*** Error: Env. var. MYID not set; quitting...")
    exit(64)

random_interval = random.random() * 60
time.sleep(random_interval)

print("myid, random_interval =",myid,random_interval)

exit(0)
