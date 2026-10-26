/*
 * End-to-end check for a real TES client (issue #7): Nextflow submits every
 * process to TESP-API through the nf-ga4gh TES executor, with the work
 * directory on S3. SHOUT reads the file GREET wrote, so the run exercises
 * S3 inputs, S3 outputs and the Nextflow task files (.command.*, .exitcode).
 */

params.outdir = 'results'

process GREET {
    output:
    path 'greeting.txt'

    script:
    """
    echo 'Hello from TESP-API' > greeting.txt
    """
}

process SHOUT {
    publishDir params.outdir, mode: 'copy'

    input:
    path greeting

    output:
    path 'shout.txt'

    script:
    """
    tr '[:lower:]' '[:upper:]' < ${greeting} > shout.txt
    """
}

workflow {
    SHOUT(GREET())
}
