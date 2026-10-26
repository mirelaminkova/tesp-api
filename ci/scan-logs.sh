#!/usr/bin/env bash
# Fails when CI logs hold presigned S3 URLs, presigned POST policies or the compose test S3 secret (issue #39), so a
# job can check its logs before printing or uploading them. tesp-api redacts these parameters name and all
# (redact_presigned_urls), so any occurrence of a name is a leak. Matches are reported by file, line and compose
# service only, never the line itself.
#
# Usage: ci/scan-logs.sh FILE_OR_DIRECTORY...
# Exit status: 0 clean, 1 leak found, 2 a file could not be read.
set -u

if [ "$#" -eq 0 ]; then
    echo "usage: $0 FILE_OR_DIRECTORY..." >&2
    exit 2
fi

# Raw or URL-encoded (policy%3D...); tesp-test-secret is TESP_API_S3__SECRET_ACCESS_KEY in docker-compose.yaml.
pattern='x-amz-signature|x-amz-credential|x-amz-security-token|awsaccesskeyid|(^|[^[:alnum:]_-])policy(=|%3d)|tesp-test-secret'

status=0
for path in "$@"; do
    # -a: Docker logs may hold binary bytes, which grep would otherwise skip.
    matches=$(LC_ALL=C grep -aEinr -- "$pattern" "$path")
    case $? in
        0)
            status=1
            # -r prefixes each match with its file name when given a directory; compose prefixes the service name.
            printf '%s\n' "$matches" | LC_ALL=C sed -nE \
                -e 's/^(([^:]*):)?([0-9]+):([[:alnum:]_.-]+ *)\|.*$/\2 line \3: \4/p; t' \
                -e 's/^(([^:]*):)?([0-9]+):.*$/\2 line \3/p' \
                | sed "s|^ line| $path line|" >&2
            ;;
        1) ;;
        *) exit 2 ;;
    esac
done

if [ "$status" -ne 0 ]; then
    echo "Presigned S3 URLs or S3 credentials found in the logs above; they must not be printed or uploaded." >&2
fi
exit "$status"
