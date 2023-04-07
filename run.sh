#!/usr/bin/env bash
set -euo pipefail

ansible-playbook ansible_iot.glatzer.eu.yml -i hosts
