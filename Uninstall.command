#!/bin/bash
cd "$(dirname "$0")"
bash ./install.sh --uninstall
echo; read -n 1 -s -r -p "Press any key to close this window."
