#!/usr/bin/env bash
# One-shot environment setup for Ubuntu 20.04 / 22.04 (VM or native).
set -euo pipefail

echo "[1/4] apt packages"
sudo apt update
sudo apt install -y mininet openvswitch-switch iproute2 iperf tcpdump \
                    python3 python3-pip python3-venv python3-matplotlib git
sudo systemctl enable --now openvswitch-switch

echo "[2/4] python virtualenv (sees the apt-installed mininet module)"
python3 -m venv --system-site-packages venv
./venv/bin/pip install --upgrade pip wheel
./venv/bin/pip install "setuptools==58.2.0"
./venv/bin/pip install -r requirements.txt

echo "[3/4] sanity checks"
sudo ovs-vsctl show >/dev/null && echo "  Open vSwitch OK"
sudo modprobe sch_netem 2>/dev/null && echo "  netem kernel module OK" || \
  echo "  !! netem missing: sudo apt install linux-modules-extra-\$(uname -r)"
./venv/bin/python -c "import ryu, mininet; print('  ryu', ryu.__version__, '+ mininet import OK')"

echo "[4/4] cleaning old Mininet state"
sudo mn -c >/dev/null 2>&1 || true
echo "Done.  Run:  sudo ./venv/bin/python run_experiment.py"
