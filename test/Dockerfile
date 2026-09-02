FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update -qq && apt-get install -y -qq \
        sudo zsh curl git ca-certificates python3 \
    && rm -rf /var/lib/apt/lists/*

RUN useradd -m -s /usr/bin/zsh tester \
    && echo 'tester ALL=(ALL) NOPASSWD:ALL' > /etc/sudoers.d/tester \
    && chmod 0440 /etc/sudoers.d/tester

USER tester
WORKDIR /home/tester
