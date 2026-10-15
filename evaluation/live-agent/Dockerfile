FROM ghcr.io/uzhw8rgl/master-thesis-agent@sha256:7c432d61d304156f4289974b5e50cde2e1035f4d0340b271bd15b15cf3618c08
COPY evaluation/live-agent/network.py /app/agent/evaluation_network.py
COPY agent/evaluation_transport.py /app/agent/evaluation_transport.py
RUN python -c 'from pathlib import Path; p=Path("/app/agent/run_agent.py"); s=p.read_text(); old="    app = FastAPI(title=\"Master Thesis Agent\")"; assert s.count(old)==1; p.write_text(s.replace(old,old+"\n    from evaluation_network import install\n    install(app)"))'
