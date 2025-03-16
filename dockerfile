FROM nvidia/cuda:11.8.0-cudnn8-runtime-ubuntu22.04

# Install system dependencies
RUN apt-get update && \
    apt-get install -y ffmpeg software-properties-common python3-dev libavcodec-extra \
    build-essential libssl-dev libffi-dev python3-pip && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

# Add application files
ADD . /app
WORKDIR /app

# Set LD_LIBRARY_PATH to help find cuDNN libraries
ENV LD_LIBRARY_PATH=/usr/local/cuda/lib64:$LD_LIBRARY_PATH

# Install Python dependencies - install PyTorch with CUDA first
RUN pip3 install --upgrade pip && \
    pip3 install torch==2.0.1+cu118 torchvision==0.15.2+cu118 torchaudio==2.0.2 --extra-index-url https://download.pytorch.org/whl/cu118 && \
    pip3 install -r requirements.txt && \
    pip3 install pydub==0.25.1 transformers accelerate>=0.26.0

# Verify package installations
RUN python3 -c "import torch; print('PyTorch CUDA available:', torch.cuda.is_available())" && \
    python3 -c "import pydub; print('pydub installation verified')" && \
    python3 -c "import transformers; print('transformers installation verified')" && \
    python3 -c "import accelerate; print('accelerate installation verified')" && \
    python3 -c "import sys; print('Python path:', sys.path)"

# Prepare startup
RUN chmod +x ./startup.sh
ENTRYPOINT ["bash", "./startup.sh"]
