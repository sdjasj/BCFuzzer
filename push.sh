#!/bin/bash

# Set a counter to track attempts
attempt=0

# Define the maximum number of retries (optional)
max_retries=100

# Define the delay between retries in seconds (optional)
delay=5

# Start the loop to push until successful
while true; do
    attempt=$((attempt + 1))
    echo "Attempt $attempt: Pushing to remote repository..."

    # Try to push
    git push
    
    # Check the exit status of the git push command
    if [ $? -eq 0 ]; then
        echo "Push successful on attempt $attempt!"
        break
    else
        echo "Push failed. Retrying in $delay seconds..."
    fi

    # Break the loop if the maximum number of retries is reached
    if [ $attempt -ge $max_retries ]; then
        echo "Reached the maximum number of retries ($max_retries). Exiting."
        exit 1
    fi

    # Wait before retrying
    sleep $delay
done
