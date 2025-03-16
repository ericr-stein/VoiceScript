#!/bin/bash

# Debug information
echo "=== Python Environment Debug Info ==="
python3 -c "import sys; print('Python path:', sys.path)"
python3 -c "import site; print('Site packages:', site.getsitepackages())"
echo "Checking for pydub module..."
python3 -c "import pydub; print('pydub successfully imported')"
echo "Checking for transformers module..."
python3 -c "import transformers; print('transformers successfully imported')"
echo "Checking for accelerate module..."
python3 -c "import accelerate; print('accelerate successfully imported')"
echo "=== End Debug Info ==="

# Start the first process
python3 worker.py &

# Start the second process
python3 main.py &

# Wait for any process to exit
wait -n

# Exit with status of process that exited first
exit $?
