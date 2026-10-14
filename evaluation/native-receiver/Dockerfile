FROM ghcr.io/uzhw8rgl/master-thesis-dfl-worker@sha256:af7edbbbdd890fce60ac7a2abbf8e205671665903945d4483ccc94ed78f31c5f
COPY evaluation/native-receiver /evaluation/native-receiver
RUN python -c 'from pathlib import Path; p=Path("/dfl/start_node_neural_network.sh"); s=p.read_text(); old="-m pki.runtime run worker -- \"${PYTHON_BIN}\" -m tee_inference.service"; assert s.count(old)==1; p.write_text(s.replace(old,"-m pki.runtime run worker -- \"${PYTHON_BIN}\" /evaluation/native-receiver/supervisor.py"))'
