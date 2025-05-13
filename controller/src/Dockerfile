FROM python:3.12-slim

# Set environment variable
ENV CONTROLLER_NAMESPACE=default

WORKDIR /usr/src/app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy operator code
COPY controller.py .

# Command to run the operator with explicit cluster-wide flag
ENTRYPOINT ["kopf", "run", "/usr/src/app/controller.py", "--verbose", "--all-namespaces"]