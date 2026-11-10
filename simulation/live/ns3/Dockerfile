# ns-3 with the LoRaWAN module (signetlabdei/lorawan), built once and reused.
#   docker build -t aqua-ns3 ns3/      (first time ~20-30 min)
# The sed adds a missing include upstream only gets via precompiled headers.
# Our programs (link_table.cc, replay.cc) are mounted at run time, see run_ns3.sh.
FROM ubuntu:24.04
RUN apt-get update && apt-get install -y --no-install-recommends g++ cmake ninja-build ccache python3 git ca-certificates \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /ns3
RUN git clone https://gitlab.com/nsnam/ns-3-dev.git . \
    && git clone --branch v0.3.7 --depth 1 https://github.com/signetlabdei/lorawan src/lorawan \
    && tag=$(cat src/lorawan/NS3-VERSION) && tag=${tag#release } && git checkout -q "$tag" \
    && sed -i '1i #include "ns3/log.h"' src/lorawan/model/logical-lora-channel.cc \
    && ./ns3 configure --build-profile=optimized --enable-modules lorawan \
    && ./ns3 build
