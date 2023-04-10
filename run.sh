#!/usr/bin/env bash
set -euo pipefail

if [ $# -eq 0 ]
then
    echo "Please provide lets encrypt email address"
    exit 1
fi

ansible-playbook ansible_iot.glatzer.eu.yml -i hosts --extra-vars="email=$1" -v
