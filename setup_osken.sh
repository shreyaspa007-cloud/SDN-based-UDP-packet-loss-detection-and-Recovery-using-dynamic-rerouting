#!/usr/bin/env bash
# Setup for NEWER Ubuntu (24.04 / 26.04, Python >= 3.11) where classic Ryu does not work.
# Uses os-ken (maintained fork of Ryu with the same API).
set -euo pipefail
sudo apt update
sudo apt install -y mininet openvswitch-switch iproute2 iperf tcpdump python3 python3-pip \
                    python3-venv python3-matplotlib git unzip
sudo systemctl enable --now openvswitch-switch
python3 -m venv --system-site-packages venv
./venv/bin/pip install --upgrade pip wheel setuptools
./venv/bin/pip install os-ken
sudo ovs-vsctl show >/dev/null && echo "Open vSwitch OK"
sudo modprobe sch_netem 2>/dev/null && echo "netem OK" || echo "!! netem missing: sudo apt install linux-modules-extra-\$(uname -r)"
./venv/bin/python -c "import os_ken, mininet; print('os-ken + mininet import OK')"
./venv/bin/python -c "from os_ken.base import app_manager; from os_ken.lib import hub; print('os-ken modules OK')"
sudo mn -c >/dev/null 2>&1 || true
echo "Done. Next: sudo ./venv/bin/python run_experiment.py --loss 30 --modes static dynamic --duration 20 --fault-at 8"
