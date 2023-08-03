"""===============
Pipeline xxx  
=====================

Overview
========

Provide a brief description of the pipeline.

Usage
=====

See :ref:`PipelineSettingUp` and :ref:`PipelineRunning` on general
information how to use CGAT pipelines.

Configuration
-------------

The pipeline requires a configured :file:`pipeline_xxx.yml` file.

A default configuration file can be generated by executing:

   python <srcdir>/pipeline_xxx.py config


Input files
-----------

Describe the required input files here.


Output files
------------

Describe the required output files here.


Glossary
========

.. glossary::


Code
====

"""
from ruffus import *

import sys
import shutil
import os

from cgatcore import experiment as E
from cgatcore import pipeline as P
import cgatcore.iotools as IOTools


# import local pipeline utility functions
import txseq.tasks as T

# ----------------------- < pipeline configuration > ------------------------ #

# Override function to collect config files
P.control.write_config_files = T.write_config_files

# load options from the yml file
P.parameters.HAVE_INITIALIZED = False
PARAMS = P.get_parameters(T.get_parameter_file(__file__))


# ---------------------- < specific pipeline tasks > ------------------------ #




@files(...,
       ...)
def task1(infile, outfile):
    '''
    Extract the splice sites
    '''

    t = T.setup(infile, outfile, PARAMS,
                memory=PARAMS["resources_memory"],
                cpu=PARAMS["resources_threads"],
                make_outdir=False)

    statement = '''
                   &> %(log_file)s
                ''' % dict(PARAMS, **t.var, **locals())

    P.run(statement, **t.resources)
    IOTools.touch_file(outfile)


@transform(task1,
       ...)
def task2(infile, outfile):
    '''
    Extract the splice sites
    '''

    t = T.setup(infile, outfile, PARAMS,
                memory=PARAMS["resources_memory"],
                cpu=PARAMS["resources_threads"],
                make_outdir=False)

    statement = '''
                   &> %(log_file)s
                ''' % dict(PARAMS, **t.var, **locals())

    P.run(statement, **t.resources)
    IOTools.touch_file(outfile)



# --------------------- < generic pipeline tasks > -------------------------- #


@follows(task2)
def full():
    '''target to run the full pipeline'''
    pass


def main(argv=None):
    if argv is None:
        argv = sys.argv
    P.main(argv)

if __name__ == "__main__":
    sys.exit(P.main(sys.argv))