##############################################################################
#
#   Kennedy Institute of Rheumatology
#
#   $Id$
#
#   Copyright (C) 2015 Stephen Sansom
#
#   This program is free software; you can redistribute it and/or
#   modify it under the terms of the GNU General Public License
#   as published by the Free Software Foundation; either version 2
#   of the License, or (at your option) any later version.
#
#   This program is distributed in the hope that it will be useful,
#   but WITHOUT ANY WARRANTY; without even the implied warranty of
#   MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#   GNU General Public License for more details.
#
#   You should have received a copy of the GNU General Public License
#   along with this program; if not, write to the Free Software
#   Foundation, Inc., 59 Temple Place - Suite 330, Boston, MA  02111-1307, USA.
###############################################################################

"""
===========================
Pipeline cram2fastq
===========================

:Author: Stephen Sansom
:Release: $Id$
:Date: |today|
:Tags: Python

Overview
========

This pipeline coverts Sanger CRAM files to fastq.gz, quality trims and reconciles the fastq files

Usage
=====

See :ref:`PipelineSettingUp` and :ref:`PipelineRunning` on general
information how to use CGAT pipelines.

Configuration
-------------

The pipeline requires a configured :file:`pipeline.ini` file.
CGATReport report requires a :file:`conf.py` and optionally a
:file:`cgatreport.ini` file (see :ref:`PipelineReporting`).

Default configuration files can be generated by executing:

   python <srcdir>/pipeline_cram2fastq.py config

Input files
-----------

Requirements
------------

The pipeline requires the results from
:doc:`pipeline_annotations`. Set the configuration variable
:py:data:`annotations_database` and :py:data:`annotations_dir`.

On top of the default CGAT setup, the pipeline requires the following
software to be in the path:

.. Add any additional external requirements such as 3rd party software
   or R modules below:

Requirements:

* samtools >= 1.1

Pipeline output
===============


Glossary
========


Code
====

"""

from ruffus import *

import sys, os, glob
import sqlite3
import CGAT.Experiment as E
import CGATPipelines.Pipeline as P
import CGATPipelines.PipelineTracks as PipelineTracks
import pysam

# load options from the config file
PARAMS = P.getParameters(
    ["%s/pipeline.ini" % os.path.splitext(__file__)[0],
     "../pipeline.ini",
     "pipeline.ini"])

# add configuration values from associated pipelines
#
# 1. pipeline_annotations: any parameters will be added with the
#    prefix "annotations_". The interface will be updated with
#    "annotations_dir" to point to the absolute path names.
PARAMS.update(P.peekParameters(
    PARAMS["annotations_dir"],
    "pipeline_annotations.py",
    on_error_raise=__name__ == "__main__",
    prefix="annotations_",
    update_interface=True))

# define some tracks if needed
TRACKS = PipelineTracks.Tracks( PipelineTracks.Sample ).loadFromDirectory(
        glob.glob("*.ini" ), "(\S+).ini" )


# -----------------------------------------------
# Utility functions
def connect():
    '''Connect to database.
       Use this method to connect to additional databases.
       Returns an sqlite3 database handle.
    '''

    dbh = sqlite3.connect(PARAMS["database"])
    statement = '''ATTACH DATABASE '%s' as annotations''' % (
        PARAMS["annotations_database"])
    cc = dbh.cursor()
    cc.execute(statement)
    cc.close()

    return dbh


# ---------------------------------------------------
# Specific pipeline tasks

    

@follows(mkdir("validate.cram.dir"))
@transform(glob.glob("data.dir/*.cram"),
           regex(r".*/(.*).cram"),
           r"validate.cram.dir/\1.validate")
def validateCramFiles(infile, outfile):
    '''Validate CRAM files by exit status of
       cramtools qstat.
    '''    

    statement = '''cramtools qstat -I %(infile)s > /dev/null;
                   echo $? > %(outfile)s;
                '''
    
    P.run()

@merge(validateCramFiles,
       "validate.cram.dir/summary.txt")
def inspectValidations(infiles, outfile):
    '''Check that all crams pass validation or
       raise an Error.'''

    outfile_handle = open(outfile, "w")

    exit_states = []
    for validation_file in infiles:
        with open(validation_file,"r") as vf_handle:
            exit_status = vf_handle.read().strip("\n")

        exit_states.append(int(exit_status))
        outfile_handle.write("\t".join([validation_file, exit_status])+"\n")

    outfile_handle.close()
    
    if sum(exit_states) != 0:
        raise ValueError("One or more cram files failed validation")


    
    
@follows(inspectValidations,
         mkdir("cell.info.dir"))
@merge(glob.glob("data.dir/*.cram"),
       "cell.info.dir/cells.txt")
def extractSampleInformation(infiles, outfile):
    '''Make a table of cells and corresponding cram files'''

    # build a dictionary of cell to cram file mappings
    cells = {}
    for cram_file in infiles:
        cram = pysam.AlignmentFile(cram_file,"rb")
        cell = cram.header["RG"][0]["SM"]
        if cell not in cells.keys():
            cells[cell] = [cram_file]
        else:
            cells[cell].append(cram_file)
        cram.close()

    # write out a per-cell list of cram files
    outdir = os.path.dirname(outfile)

    outfile_handle = open(outfile, "w")
    outfile_handle.write("#cell\tcram_files\n")

    for cell in cells.keys():
        outfile_handle.write("\t".join([cell,",".join(cells[cell])])+"\n")

    outfile_handle.close()



@split(extractSampleInformation,
       "cell.info.dir/*.cell")
def cellCramLists(infile, outfiles):
    '''Make a per-cell file containing the cram file(s)
       corresponding to the cell'''

    out_dir = os.path.dirname(infile)
    
    with open(infile,"r") as cell_list:
        for record in cell_list:
            if record.startswith("#"):
                continue
            cell, cram_list = record.strip("\n").split("\t")
            crams = cram_list.split(",")
            with open(os.path.join(out_dir,cell+".cell"),"w") as cell_file_handle:
                for cram in crams:
                    cell_file_handle.write(cram+"\n")

   
@follows(mkdir("fastq.dir"),
         mkdir("fastq.temp.dir"),
         extractSampleInformation)
@transform(cellCramLists,
           regex(r".*/(.*).cell"),
           (r"fastq.dir/\1.fastq.1.gz",
            r"fastq.dir/\1.fastq.2.gz"))
def cram2fastq(infile, outfiles):
    '''Convert Sanger CRAM files to Fastq format
       Takes care of merging, quality trimming
       and pair reconciliation.
       Intermediate files are not kept by default.'''

    ## TODO: make quality trimming optional.

    ###################################
    # set variables and open a log file
    ###################################
    
    cell_name = os.path.basename(infile)[:-len(".cell")]
    out_dir = os.path.dirname(outfiles[0])
    temp_dir = "fastq.temp.dir"
    
    log_file = os.path.join(temp_dir,
                            cell_name + ".fastq.extraction.log")

    log = open(log_file,"w")
    log.write("Fastq extraction log file for %(infile)s\n\n")

    def _merge_dicts(a, b):
        x = a.copy()
        x.update(b)
        return(x)

    temp_files = []
    
    #################################################
    # Extract per-end Fastq(s) from the cram file(s)
    #################################################
    
    raw_fastq_names = []
    with open(infile, "rb") as cram_files:

        for line in cram_files:
          
            cram = line.strip()
            raw_fastq_name = os.path.join(temp_dir,
                                     os.path.basename(cram)[:-len(".cram")] )
            raw_fastq_names.append(raw_fastq_name)

            statement = '''cramtools fastq --enumerate
                                       -F %(raw_fastq_name)s 
                                       -I %(cram)s
                                       --gzip
                         '''
            log.write("Extracting fastqs from %(cram)s:" % locals() + "\n")
            log.write(statement % locals() + "\n")
            
            P.run()

            log.write("done.\n\n")

            
    #####################################          
    # Perform quality trimming
    # Merging is also taken care of here.
    #####################################
    
    quality = PARAMS["preprocess_quality_threshold"]
    minlen = PARAMS["preprocess_min_length"]

    trimmed_fastq_prefix = os.path.join(temp_dir, cell_name)
    

    trimmed_fastq_files = []
    # fastq(s) for each end are quality trimmed separately
    for end in ["_1","_2"]:

        raw_fastqs = [x + end + ".fastq.gz" for x in raw_fastq_names]
        temp_files += raw_fastqs
        
        fastq_list = " ".join(raw_fastqs)

        trimmed_fastq_name = trimmed_fastq_prefix + end + ".trimmed.fastq.gz"
        trimmed_fastq_files.append(trimmed_fastq_name)

        log.write(">> Quality trimming %(fastq_list)s: " % locals() + "\n")
        statement = '''zcat %(fastq_list)s
                       | fastq_quality_trimmer 
                           -Q33
                           -t %(quality)s
                           -l %(minlen)s
                       | gzip -c
                       > %(trimmed_fastq_name)s
                    '''
        log.write(statement % _merge_dicts(PARAMS,locals()) + "\n")
        P.run()
        log.write("done. \n\n")

    temp_files += trimmed_fastq_files
        
    ####################    
    # Reconcile the ends
    ####################
    
    end1, end2 = trimmed_fastq_files

    reconciled_fastq_prefix = outfiles[0][:-len(".1.gz")]

    log.write(">> Reconciling pairs, %(end1)s & %(end2)s: " % locals() + "\n")
    statement='''python %(scriptsdir)s/fastqs2fastqs.py
                 %(end1)s %(end2)s
                 --method reconcile
                 --chop
                 --unpaired
                 -o "%(reconciled_fastq_prefix)s.%%s.gz";
              '''
    log.write(statement % _merge_dicts(PARAMS, locals()) + "\n")
    P.run()
    log.write("done\n\n")

    ##############################
    # Clean up the temporary files
    ##############################

    if PARAMS["keep_temporary"]==0:
        
        temp_file_list = " ".join(temp_files)

        # record files sizes and md5 checksums of the temporary files
        log.write(">> Recording sizes and checksums of temporary files:\n")
        statement = '''ls -l %(temp_file_list)s
                       > %(temp_dir)s/%(cell_name)s.ls;
                       checkpoint;
                       md5sum %(temp_file_list)s 
                       > %(temp_dir)s/%(cell_name)s.md5;
                    '''
        log.write(statement % locals() + "\n")
        P.run()
        log.write("done\n\n")

        # unlink (delete) the temporary files
        log.write(">> unlinking temporary files: " + temp_file_list + "\n")

        for temp_file in temp_files:
            os.unlink(temp_file)

        log.write("tempororay files unlinked\n")
    
    log.close()
    

# ---------------------------------------------------
# Generic pipeline tasks
follows(cram2fastq)
def full():
    pass

###########################################################################

if __name__ == "__main__":
    sys.exit(P.main(sys.argv))
