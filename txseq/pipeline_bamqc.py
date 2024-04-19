"""==================
Pipeline bam_qc.py
==================

Overview
--------

This pipeline computes QC statistic from BAM files. It uses the `Picard toolkit <https://broadinstitute.github.io/picard/>`_ and some custom scripts.


Configuration
-------------

The pipeline requires a configured :file:`pipeline_bam_qc.yml` file.

Default configuration files can be generated by executing: ::

   txseq pipeline_bam_qc config


Inputs
------

The pipeline requires the following inputs

#. samples.tsv: see :doc:`Configuration files<configuration>`
#. bam files: the location of a folder containing the bam files named by "sample_id".
#. txseq annotations: the location where the :doc:`pipeline_ensembl.py </pipelines/pipeline_ensembl>` was run to prepare the annotatations.


Requirements
------------

The following software is required:

#. Picard

Output files
------------

The pipeline produces the following outputs:

#. Picard rnaseq metrics: in the bam.qc.dir/rnaseq.metrics.dir
#. Picard alignment summary metrics: in the bam.qc.dir/alignment.summary.metrics.dir
#. Fraction of spliced reads: in the bam.qc.dir/fraction.spliced.dir
#. An sqlite database: in a file named "csvdb" which contains tables of the QC metrics, with key metrics summarised in the "qc_summary" table.


Code
====

"""
from ruffus import *

import sys
import shutil
import os
from pathlib import Path
import glob
import sqlite3

import pandas as pd
import numpy as np

from cgatcore import experiment as E
from cgatcore import pipeline as P
from cgatcore import database as DB
import cgatcore.iotools as IOTools


# import local pipeline utility functions
import txseq.tasks as T

# ----------------------- < pipeline configuration > ------------------------ #

# Override function to collect config files
P.control.write_config_files = T.write_config_files

# load options from the yml file
P.parameters.HAVE_INITIALIZED = False
PARAMS = P.get_parameters(T.get_parameter_file(__file__))

# set the location of the code directory
PARAMS["txseq_code_dir"] = Path(__file__).parents[1]

PAIRED = False

if len(sys.argv) > 1:
    if(sys.argv[1] == "make"):
        
        S = T.samples(sample_tsv = PARAMS["samples"],
                            library_tsv = None)
        
        if S.npaired > 0: PAIRED = True
        
        # Set the database locations
        DATABASE = PARAMS["sqlite"]["file"]
        

# ---------------------- < specific pipeline tasks > ------------------------ #

# ------------------------- Geneset Definition ------------------------------ #

@follows(mkdir("annotations.dir"))
@files(None,
       "annotations.dir/geneset.flat.sentinel")
def flatGeneset(infile, sentinel):
    '''
    Prepare a flat version of the geneset
    for the Picard CollectRnaSeqMetrics module.
    '''

    t = T.setup(infile, sentinel, PARAMS,
            memory="4G",
            cpu=1)

    gtf_path = os.path.join(PARAMS["txseq_annotations"],
                            "api.dir/txseq.geneset.gtf.gz")
    
    if not os.path.exists(gtf_path):
        raise ValueError("txseq annotations GTF file not found")
    
    outfile = sentinel.replace(".sentinel", ".gz")

    statement = '''gtfToGenePred
                    -genePredExt
                    -geneNameAsName2
                    -ignoreGroupsWithoutExons
                    %(gtf_path)s
                    /dev/stdout |
                    awk 'BEGIN { OFS="\\t"}
                         {print $12, $1, $2, $3, $4, $5, $6, $7, $8, $9, $10}'
                    | gzip -c
                    > %(outfile)s
                 '''

    P.run(statement, **t.resources)
    IOTools.touch_file(sentinel)


# ------------------- Picard: CollectRnaSeqMetrics -------------------------- #


def collect_rna_seq_metrics_jobs():

    for sample_id in S.samples.keys():
    
        yield([os.path.join(PARAMS["bam_path"], sample_id + ".bam"),
                os.path.join("bam.qc.dir/rnaseq.metrics.dir/",
                            sample_id + ".rnaseq.metrics.sentinel")])

@follows(flatGeneset)
@files(collect_rna_seq_metrics_jobs)
def collectRnaSeqMetrics(infile, sentinel):
    '''
    Run Picard CollectRnaSeqMetrics on the bam files.
    '''

    t = T.setup(infile, sentinel, PARAMS,
            memory=PARAMS["picard_memory"],
            cpu=PARAMS["picard_threads"])

    bam_file = infile
    geneset_flat = "annotations.dir/geneset.flat.gz"
    
    sample_id = os.path.basename(bam_file)[:-len(".bam")]
    sample = S.samples[sample_id]
    picard_strand = sample.picard_strand

    if PARAMS["picard_collectrnaseqmetrics_options"]:
        picard_options = PARAMS["picard_collectrnaseqmetrics_options"]
    else:
        picard_options = ""

    validation_stringency = PARAMS["picard_validation_stringency"]

    coverage_out = t.out_file[:-len(".metrics")] + ".cov.hist"
    chart_out = t.out_file[:-len(".metrics")] + ".cov.pdf"

    mktemp_template = "ctmp.CollectRnaSeqMetrics.XXXXXXXXXX"

    statement = '''picard_out=`mktemp -p . %(mktemp_template)s`;
                   %(picard_cmd)s CollectRnaSeqMetrics
                   -I %(bam_file)s
                   --REF_FLAT %(geneset_flat)s
                   -O $picard_out
                   --CHART %(chart_out)s
                   --STRAND_SPECIFICITY %(picard_strand)s
                   --VALIDATION_STRINGENCY %(validation_stringency)s
                   %(picard_options)s;
                   grep . $picard_out | grep -v "#" | head -n2
                   > %(out_file)s;
                   grep . $picard_out
                   | grep -A 102 "## HISTOGRAM"
                   | grep -v "##"
                   > %(coverage_out)s;
                   rm $picard_out;
                ''' % dict(PARAMS, **t.var, **locals())

    P.run(statement, **t.resources)
    IOTools.touch_file(sentinel)


@merge(collectRnaSeqMetrics,
       "bam.qc.dir/qc_rnaseq_metrics.load")
def loadCollectRnaSeqMetrics(infiles, outfile):
    '''
    Load the metrics to the db.
    '''
    
    infiles = [x.replace(".sentinel", "") for x in infiles]

    P.concatenate_and_load(infiles, outfile,
                           regex_filename=".*/.*/(.*).rnaseq.metrics",
                           cat="sample_id",
                           options='-i "sample_id"')


# --------------------- Three prime bias analysis --------------------------- #

@transform(collectRnaSeqMetrics,
           suffix(".rnaseq.metrics.sentinel"),
           ".three.prime.bias")
def threePrimeBias(infile, outfile):
    '''
    Compute a sensible three prime bias metric
    from the picard coverage histogram.
    '''

    infile = infile.replace(".sentinel", "")

    coverage_histogram = infile[:-len(".metrics")] + ".cov.hist"

    df = pd.read_csv(coverage_histogram, sep="\t")

    x = "normalized_position"
    cov = "All_Reads.normalized_coverage"

    three_prime_coverage = np.mean(df[cov][(df[x] > 70) & (df[x] < 90)])
    transcript_body_coverage = np.mean(df[cov][(df[x] > 20) & (df[x] < 90)])
    bias = three_prime_coverage / transcript_body_coverage

    with open(outfile, "w") as out_file:
        out_file.write("three_prime_bias\n")
        out_file.write("%.2f\n" % bias)


@merge(threePrimeBias,
       "bam.qc.dir/qc_three_prime_bias.load")
def loadThreePrimeBias(infiles, outfile):
    '''
    Load the metrics in the project database.
    '''

    P.concatenate_and_load(infiles, outfile,
                           regex_filename=".*/.*/(.*).three.prime.bias",
                           cat="sample_id",
                           options='-i "sample_id"')


# ----------------- Picard: EstimateLibraryComplexity ----------------------- #


def estimate_library_complexity_jobs():

    for sample_id in S.samples.keys():
    
        if  S.samples[sample_id].paired == True:
    
            yield([os.path.join(PARAMS["bam_path"], sample_id + ".bam"),
                   os.path.join("bam.qc.dir/estimate.library.complexity.dir/",
                                sample_id + ".library.complexity.sentinel")])

@active_if(PAIRED and PARAMS["run_estimateLibraryComplexity"])
@files(estimate_library_complexity_jobs)
def estimateLibraryComplexity(infile, sentinel):
    '''
    Run Picard EstimateLibraryComplexity on the BAM files.
    '''
    t = T.setup(infile, sentinel, PARAMS,
        memory=PARAMS["picard_memory"],
        cpu=PARAMS["picard_threads"])

    if PARAMS["picard_estimatelibrarycomplexity_options"]:
        picard_options = PARAMS["picard_estimatelibrarycomplexity_options"]
    else:
        picard_options = ""

    validation_stringency = PARAMS["picard_validation_stringency"]

    mktemp_template = "ctmp.EstimateLibraryComplexity.XXXXXXXXXX"

    statement = '''picard_out=`mktemp -p . %(mktemp_template)s`;
                   %(picard_cmd)s EstimateLibraryComplexity
                   -I %(infile)s
                   -O $picard_out
                   --VALIDATION_STRINGENCY %(validation_stringency)s
                   %(picard_options)s;
                   grep . $picard_out | grep -v "#" | head -n2
                   > %(out_file)s;
                   rm $picard_out;
                ''' % dict(PARAMS, **t.var, **locals())

    P.run(statement, **t.resources)
    IOTools.touch_file(sentinel)
    

@active_if(PAIRED and PARAMS["run_estimateLibraryComplexity"])
@merge(estimateLibraryComplexity,
       "bam.qc.dir/qc_library_complexity.load")
def loadEstimateLibraryComplexity(infiles, outfile):
    '''
    Load the complexity metrics to a single table in the project database.
    '''
    
    infiles = [x.replace(".sentinel", "") for x in infiles]

    P.concatenate_and_load(infiles, outfile,
                           regex_filename=".*/.*/(.*).library.complexity",
                           cat="sample_id",
                           options='-i "sample_id"')



# ------------------- Picard: AlignmentSummaryMetrics ----------------------- #


def alignment_summary_metrics_jobs():

    for sample_id in S.samples.keys():
    
        yield([os.path.join(PARAMS["bam_path"], sample_id + ".bam"),
                os.path.join("bam.qc.dir/alignment.summary.metrics.dir/",
                            sample_id + ".alignment.summary.metrics.sentinel")])

@files(alignment_summary_metrics_jobs)
def alignmentSummaryMetrics(infile, sentinel):
    '''
    Run Picard AlignmentSummaryMetrics on the bam files.
    '''

    t = T.setup(infile, sentinel, PARAMS,
            memory=PARAMS["picard_memory"],
            cpu=PARAMS["picard_threads"])

    picard_options = PARAMS["picard_alignmentsummarymetric_options"]
    validation_stringency = PARAMS["picard_validation_stringency"]

    reference_sequence = os.path.join(PARAMS["txseq_annotations"],
                                      "api.dir/txseq.genome.fa.gz")
    
    if not os.path.exists(reference_sequence):
        raise ValueError("Reference sequence not found")

    mktemp_template = "ctmp.CollectAlignmentSummaryMetrics.XXXXXXXXXX"

    statement = '''picard_out=`mktemp -p . %(mktemp_template)s`;
                   %(picard_cmd)s CollectAlignmentSummaryMetrics
                   -I %(infile)s
                   -O $picard_out
                   --REFERENCE_SEQUENCE %(reference_sequence)s
                   --VALIDATION_STRINGENCY %(validation_stringency)s
                   %(picard_options)s;
                   sed -e '1,/## HISTOGRAM/!d' $picard_out
                   | grep . | grep -v "#"
                   > %(out_file)s;
                   rm $picard_out;
                ''' % dict(PARAMS, **t.var, **locals())

    P.run(statement, **t.resources)
    IOTools.touch_file(sentinel)


@merge(alignmentSummaryMetrics,
       "bam.qc.dir/qc_alignment_summary_metrics.load")
def loadAlignmentSummaryMetrics(infiles, outfile):
    '''
    Load the complexity metrics to a single table in the project database.
    '''

    infiles = [x.replace(".sentinel", "") for x in infiles]

    P.concatenate_and_load(
        infiles, outfile,
        regex_filename=".*/.*/(.*).alignment.summary.metrics",
        cat="sample_id",
        options='-i "sample_id"')


# ------------------- Picard: InsertSizeMetrics ----------------------- #

def insert_size_jobs():

    for sample_id in S.samples.keys():
    
        if  S.samples[sample_id].paired == True:
    
            yield([os.path.join(PARAMS["bam_path"], sample_id + ".bam"),
                   [os.path.join("bam.qc.dir/insert.size.metrics.dir/",
                                sample_id + ".insert.size.metrics.summary.sentinel"),
                    os.path.join("bam.qc.dir/insert.size.metrics.dir/",
                                sample_id + ".insert.size.metrics.histogram.sentinel"),
                   ]])

@active_if(PAIRED)
@files(insert_size_jobs)
def insertSizeMetricsAndHistograms(infile, sentinels):
    '''
    Run Picard InsertSizeMetrics on the BAM files to
    collect summary metrics and histograms.'''

    t = T.setup(infile, sentinels[0], PARAMS,
            memory=PARAMS["picard_memory"],
            cpu=PARAMS["picard_threads"])

    picard_summary, picard_histogram = [ x.replace(".sentinel", "") for x in sentinels ]
    picard_histogram_pdf = picard_histogram + ".pdf"

    if PARAMS["picard_insertsizemetric_options"]:
        picard_options = PARAMS["picard_insertsizemetric_options"]
    else:
        picard_options = ""

    validation_stringency = PARAMS["picard_validation_stringency"]
    
    reference_sequence = os.path.join(PARAMS["txseq_annotations"],
                                      "api.dir/txseq.genome.fa.gz")
    
    if not os.path.exists(reference_sequence):
        raise ValueError("Reference sequence not found")

    mktemp_template = "ctmp.CollectInsertSizeMetrics.XXXXXXXXXX"

    statement = '''picard_out=`mktemp -p . %(mktemp_template)s`;
                   %(picard_cmd)s CollectInsertSizeMetrics
                   -I %(infile)s
                   -O $picard_out
                   --Histogram_FILE %(picard_histogram_pdf)s
                   --VALIDATION_STRINGENCY %(validation_stringency)s
                   --REFERENCE_SEQUENCE %(reference_sequence)s
                   %(picard_options)s;
                   grep "MEDIAN_INSERT_SIZE" -A 1 $picard_out
                   > %(picard_summary)s;
                   sed -e '1,/## HISTOGRAM/d' $picard_out
                   > %(picard_histogram)s;
                   rm $picard_out;
                ''' % dict(PARAMS, **t.var, **locals())

    P.run(statement, **t.resources)
    
    for sentinel in sentinels: 
        IOTools.touch_file(sentinel)

@active_if(PAIRED)
@merge(insertSizeMetricsAndHistograms,
       "bam.qc.dir/qc_insert_size_metrics.load")
def loadInsertSizeMetrics(infiles, outfile):
    '''
    Load the insert size metrics to a single table of the project database.
    '''

    picard_summaries = [x[0].replace(".sentinel", "") for x in infiles]

    P.concatenate_and_load(picard_summaries, outfile,
                            regex_filename=(".*/.*/(.*)"
                                            ".insert.size.metrics.summary"),
                            cat="sample_id",
                            options='')


@active_if(PAIRED)
@merge(insertSizeMetricsAndHistograms,
       "bam.qc.dir/qc_insert_size_histogram.load")
def loadInsertSizeHistograms(infiles, outfile):
    '''
    Load the histograms to a single table of the project database.
    '''

    picard_histograms = [x[1].replace(".sentinel", "") for x in infiles]

    P.concatenate_and_load(
        picard_histograms, outfile,
        regex_filename=(".*/.*/(.*)"
                        ".insert.size.metrics.histogram"),
        cat="sample_id",
        options='-i "insert_size" -e')




# --------------------- Fraction of spliced reads --------------------------- #


def fraction_spliced_jobs():

    for sample_id in S.samples.keys():
    
        yield([os.path.join(PARAMS["bam_path"], sample_id + ".bam"),
                os.path.join("bam.qc.dir/fraction.spliced.dir/",
                            sample_id + ".fraction.spliced.sentinel")])

@files(fraction_spliced_jobs)
def fractionSpliced(infile, sentinel):
    '''
    Compute fraction of reads containing a splice junction.
    * paired-endedness is ignored
    * only uniquely mapping reads are considered.
    '''
    
    t = T.setup(infile, sentinel, PARAMS)

    statement = '''echo "fraction_spliced" > %(out_file)s;
                   samtools view %(infile)s
                   | grep NH:i:1
                   | cut -f 6
                   | awk '{if(index($1,"N")==0){us+=1}
                           else{s+=1}}
                          END{print s/(us+s)}'
                   >> %(out_file)s
                 ''' % dict(PARAMS, **t.var, **locals())

    P.run(statement, **t.resources)
    IOTools.touch_file(sentinel)


@merge(fractionSpliced,
       "bam.qc.dir/qc_fraction_spliced.load")
def loadFractionSpliced(infiles, outfile):
    '''
    Load fractions of spliced reads to a single table of the project database.
    '''
    
    infiles = [x.replace(".sentinel","") for x in infiles]

    P.concatenate_and_load(infiles, outfile,
                           regex_filename=".*/.*/(.*).fraction.spliced",
                           cat="sample_id",
                           options='-i "sample_id"')


# ---------------- Prepare a post-mapping QC summary ------------------------ #


@files(PARAMS["samples"],
           "samples.load")
def loadSampleInformation(infile, outfile):
    '''
    Load the sample information table to the project database.
    '''

    P.load(infile, outfile, options='-i "sample_id"')


@merge([loadSampleInformation,
        loadCollectRnaSeqMetrics,
        loadThreePrimeBias,
        loadEstimateLibraryComplexity,
        loadFractionSpliced,
        loadAlignmentSummaryMetrics,
        loadInsertSizeMetrics],
       "bam.qc.dir/qc_summary.txt")
def qcSummary(infiles, outfile):
    '''
    Create a summary table of relevant QC metrics.
    '''

    # Some QC metrics are specific to paired end data
    if PAIRED:
        exclude = []
        paired_columns = '''PCT_READS_ALIGNED_IN_PAIRS
                                       as pct_reads_aligned_in_pairs,
                              MEDIAN_INSERT_SIZE
                                       as median_insert_size,
                           '''
        pcat = "PAIR"
   
    else:
        exclude = ["qc_library_complexity", "qc_insert_size_metrics"]
        paired_columns = ''
        pcat = "UNPAIRED"

    if PARAMS["run_estimateLibraryComplexity"] and PAIRED:
        elc_columns = '''ESTIMATED_LIBRARY_SIZE as library_size,
                         READ_PAIRS_EXAMINED as no_pairs,
                         PERCENT_DUPLICATION as pct_duplication,
        '''

    else:
        elc_columns = ''
   

    # ESTIMATED_LIBRARY_SIZE as library_size,

    tables = [P.to_table(x) for x in infiles
              if P.to_table(x) not in exclude]

    t1 = tables[0]

    stat_start = '''select distinct samples.*,
                                    fraction_spliced,
                                    three_prime_bias
                                       as three_prime_bias,
                                    %(paired_columns)s
                                    %(elc_columns)s
                                    PCT_MRNA_BASES
                                       as pct_mrna,
                                    PCT_CODING_BASES
                                       as pct_coding,
                                    PCT_PF_READS_ALIGNED
                                       as pct_reads_aligned,
                                    TOTAL_READS
                                       as total_reads,
                                    PCT_ADAPTER
                                       as pct_adapter,
                                    PF_HQ_ALIGNED_READS*1.0/PF_READS
                                       as pct_pf_reads_aligned_hq
                   from %(t1)s
                ''' % locals()

    join_stat = ""
    for table in tables[1:]:
        join_stat += "left join " + table + "\n"
        join_stat += "on " + t1 + ".sample_id=" + table + ".sample_id\n"

    where_stat = '''where qc_alignment_summary_metrics.CATEGORY="%(pcat)s"
                 ''' % locals()

    statement = "\n".join([stat_start, join_stat, where_stat])

    df = DB.fetch_DataFrame(statement, PARAMS["sqlite_file"])
    df.to_csv(outfile, sep="\t", index=False)


@transform(qcSummary,
           suffix(".txt"),
           ".load")
def loadQCSummary(infile, outfile):
    '''
    Load summary to project database.
    '''

    P.load(infile, outfile)


@follows(loadQCSummary, loadInsertSizeHistograms)
def qc():
    '''
    Target for executing quality control.
    '''
    pass


# --------------------- < generic pipeline tasks > -------------------------- #


@follows(qc)
def full():
    pass


print(sys.argv)

def main(argv=None):
    if argv is None:
        argv = sys.argv
    P.main(argv)

if __name__ == "__main__":
    sys.exit(P.main(sys.argv))

