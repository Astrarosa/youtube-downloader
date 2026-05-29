# Use an official lightweight Python runtime as a parent image
FROM python:3.10-slim

# Install system dependencies (including ffmpeg for merging video & audio formats)
RUN apt-get update && apt-get install -y \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# Set the working directory inside the cloud container
WORKDIR /app

# Copy the requirements file and install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of your local application files over
COPY . .

# Inform Render about the port configuration
EXPOSE 8000

# Run the Python script when the container launches
CMD ["python", "YTDLDRWK.py"]