FROM nvidia/cuda:12.4.1-cudnn-runtime-ubuntu22.04

# Install system dependencies
RUN apt-get update && \
    apt-get install -y ffmpeg software-properties-common python3-dev libavcodec-extra \
    build-essential libssl-dev libffi-dev python3-pip && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

# Add application files
ADD . /app
WORKDIR /app

# Install Python dependencies
RUN pip3 install --upgrade pip && \
    pip3 install -r requirements.txt && \
    pip3 install pydub==0.25.1 transformers accelerate>=0.26.0

# Verify package installations
RUN python3 -c "import pydub; print('pydub installation verified')" && \
    python3 -c "import transformers; print('transformers installation verified')" && \
    python3 -c "import accelerate; print('accelerate installation verified')" && \
    python3 -c "import sys; print('Python path:', sys.path)"

# Prepare startup
RUN chmod +x ./startup.sh
ENTRYPOINT ["bash", "./startup.sh"]
