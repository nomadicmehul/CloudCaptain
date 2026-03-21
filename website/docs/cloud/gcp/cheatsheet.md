---
title: "GCP gcloud Cheatsheet"
description: "80+ essential gcloud CLI commands organized by service"
sidebar_label: "Cheatsheet"
sidebar_position: 4
---

# GCP gcloud CLI Cheatsheet

## Configuration and Setup

```bash
# Initialize gcloud
gcloud init

# List accounts
gcloud auth list

# Login to an account
gcloud auth login

# Set active account
gcloud auth set account <your_email@gmail.com>

# Set default project
gcloud config set project PROJECT_ID

# List configuration
gcloud config list

# Show current configuration
gcloud config list --all

# Set default zone
gcloud config set compute/zone us-central1-a

# Set default region
gcloud config set compute/region us-central1

# Update components
gcloud components update

# Install components
gcloud components install kubectl

# Update auth application-default credentials
gcloud auth application-default login
```

## Project and Organization Management

```bash
# List all projects
gcloud projects list

# List projects in JSON format
gcloud projects list --format="json"

# Get project details
gcloud projects describe PROJECT_ID

# Create a new project
gcloud projects create PROJECT_ID --name="My Project"

# Set a project as active
gcloud config set project PROJECT_ID

# Delete a project
gcloud projects delete PROJECT_ID

# List organization
gcloud organizations list

# Get organization information
gcloud organizations describe ORG_ID
```

## Compute Engine (VMs)

### Instance Management

```bash
# Create a VM instance
gcloud compute instances create my-instance \
  --image-family ubuntu-2004-lts \
  --image-project ubuntu-os-cloud \
  --zone us-central1-a \
  --machine-type e2-medium

# Create instance with startup script
gcloud compute instances create my-instance \
  --image-family ubuntu-2004-lts \
  --image-project ubuntu-os-cloud \
  --metadata startup-script='#!/bin/bash
echo "Hello World" > /var/www/html/index.html'

# List all instances
gcloud compute instances list

# Describe an instance
gcloud compute instances describe my-instance --zone us-central1-a

# Start an instance
gcloud compute instances start my-instance --zone us-central1-a

# Stop an instance
gcloud compute instances stop my-instance --zone us-central1-a

# Restart an instance
gcloud compute instances restart my-instance --zone us-central1-a

# Delete an instance
gcloud compute instances delete my-instance --zone us-central1-a

# SSH into an instance
gcloud compute ssh my-instance --zone us-central1-a

# Copy file to instance
gcloud compute scp myfile.txt my-instance:~/

# Copy file from instance
gcloud compute scp my-instance:~/myfile.txt ./

# Get instance details
gcloud compute instances describe my-instance --zone us-central1-a
```

### Machine Types and Sizing

```bash
# List available machine types
gcloud compute machine-types list

# List machine types in a zone
gcloud compute machine-types list --filter="zone:us-central1-a"

# Show machine type details
gcloud compute machine-types describe e2-medium --zone us-central1-a
```

### Images and Snapshots

```bash
# List available images
gcloud compute images list

# List custom images
gcloud compute images list --filter="family:custom"

# Create image from instance
gcloud compute images create my-image --source-disk=my-instance

# Create snapshot of disk
gcloud compute disks-snapshots create my-snapshot --source-disk=my-disk

# List snapshots
gcloud compute snapshots list

# Delete snapshot
gcloud compute snapshots delete my-snapshot

# Create disk from snapshot
gcloud compute disks create restored-disk --source-snapshot=my-snapshot
```

### Firewall Rules

```bash
# Create firewall rule (allow HTTP/HTTPS)
gcloud compute firewall-rules create allow-web \
  --allow=tcp:80,tcp:443 \
  --source-ranges=0.0.0.0/0 \
  --network=default

# Create firewall rule (allow SSH)
gcloud compute firewall-rules create allow-ssh \
  --allow=tcp:22 \
  --source-ranges=203.0.113.0/32

# List firewall rules
gcloud compute firewall-rules list

# Describe firewall rule
gcloud compute firewall-rules describe allow-web

# Delete firewall rule
gcloud compute firewall-rules delete allow-web

# Update firewall rule
gcloud compute firewall-rules update allow-web \
  --allow=tcp:80,tcp:443,tcp:3000
```

## Cloud Storage (GCS)

```bash
# Create a bucket (globally unique name)
gsutil mb gs://my-unique-bucket-name/

# List all buckets
gsutil ls

# List bucket contents
gsutil ls gs://my-bucket/

# List bucket contents recursively
gsutil ls -r gs://my-bucket/

# Upload file to bucket
gsutil cp myfile.txt gs://my-bucket/

# Upload directory to bucket
gsutil -m cp -r ./local-dir gs://my-bucket/

# Download file from bucket
gsutil cp gs://my-bucket/myfile.txt ./

# Copy between buckets
gsutil cp gs://source-bucket/file.txt gs://dest-bucket/

# Delete file from bucket
gsutil rm gs://my-bucket/myfile.txt

# Delete all files in bucket
gsutil -m rm -r gs://my-bucket/*

# Delete bucket
gsutil rb gs://my-bucket/

# Set bucket lifecycle policy
gsutil lifecycle set lifecycle.json gs://my-bucket/

# Set default storage class
gsutil defstorageclass set NEARLINE gs://my-bucket/

# Set bucket versioning
gsutil versioning set on gs://my-bucket/

# Check bucket size
gsutil du -s gs://my-bucket/

# Make object public
gsutil acl ch -u AllUsers:R gs://my-bucket/myfile.txt

# Remove public access
gsutil acl ch -d AllUsers gs://my-bucket/myfile.txt

# Set object cache control
gsutil -h "Cache-Control:public, max-age=3600" cp myfile.txt gs://my-bucket/
```

## Kubernetes Engine (GKE)

```bash
# Enable GKE API
gcloud services enable container.googleapis.com

# List GKE clusters
gcloud container clusters list

# Create GKE cluster
gcloud container clusters create my-cluster \
  --zone us-central1-a \
  --num-nodes 3 \
  --machine-type e2-medium \
  --enable-autorepair \
  --enable-autoupgrade \
  --release-channel regular

# Get cluster credentials
gcloud container clusters get-credentials my-cluster \
  --zone us-central1-a

# Describe cluster
gcloud container clusters describe my-cluster --zone us-central1-a

# Delete cluster
gcloud container clusters delete my-cluster --zone us-central1-a

# Resize cluster
gcloud container clusters resize my-cluster \
  --num-nodes 5 \
  --zone us-central1-a

# Create node pool
gcloud container node-pools create high-memory \
  --cluster=my-cluster \
  --machine-type n1-highmem-4 \
  --zone us-central1-a

# List node pools
gcloud container node-pools list --cluster=my-cluster

# Delete node pool
gcloud container node-pools delete high-memory \
  --cluster=my-cluster \
  --zone us-central1-a
```

## Cloud Run

```bash
# Enable Cloud Run API
gcloud services enable run.googleapis.com

# Deploy to Cloud Run
gcloud run deploy myservice \
  --image gcr.io/PROJECT-ID/myservice \
  --platform managed \
  --region us-central1 \
  --memory 512Mi \
  --timeout 300

# List Cloud Run services
gcloud run services list

# Describe a service
gcloud run services describe myservice --region us-central1

# Delete a service
gcloud run services delete myservice --region us-central1

# Update service traffic (canary deployment)
gcloud run services update-traffic myservice \
  --to-revisions REVISION1=80,REVISION2=20

# Get service URL
gcloud run services describe myservice --region us-central1 \
  --format='value(status.url)'

# Add IAM policy binding (service-to-service auth)
gcloud run services add-iam-policy-binding myservice \
  --member=serviceAccount:sa@PROJECT-ID.iam.gserviceaccount.com \
  --role=roles/run.invoker

# Set minimum instances
gcloud run services update myservice \
  --min-instances 1 \
  --region us-central1
```

## Cloud Functions

```bash
# Enable Cloud Functions API
gcloud services enable cloudfunctions.googleapis.com

# Deploy a function (HTTP trigger)
gcloud functions deploy my-function \
  --entry-point myFunction \
  --runtime python39 \
  --trigger-http \
  --allow-unauthenticated

# Deploy a function (Cloud Storage trigger)
gcloud functions deploy my-function \
  --entry-point myFunction \
  --runtime python39 \
  --trigger-resource my-bucket \
  --trigger-event google.storage.object.finalize

# Deploy a function (Pub/Sub trigger)
gcloud functions deploy my-function \
  --entry-point myFunction \
  --runtime python39 \
  --trigger-topic my-topic

# List functions
gcloud functions list

# Describe function
gcloud functions describe my-function

# Call function locally
gcloud functions call my-function --data '{"name":"World"}'

# Delete function
gcloud functions delete my-function

# Get function logs
gcloud functions logs read my-function --limit 50

# Update environment variables
gcloud functions deploy my-function \
  --update-env-vars KEY=value
```

## Cloud Build

```bash
# Enable Cloud Build API
gcloud services enable cloudbuild.googleapis.com

# Submit a build
gcloud builds submit \
  --tag gcr.io/PROJECT-ID/myapp

# Submit build with specific config
gcloud builds submit \
  --config cloudbuild.yaml

# List builds
gcloud builds list

# Get build logs
gcloud builds log BUILD_ID

# Cancel a build
gcloud builds cancel BUILD_ID

# Create a trigger (GitHub)
gcloud builds triggers create github \
  --repo-name=my-repo \
  --repo-owner=my-username \
  --branch-pattern="^main$" \
  --build-config=cloudbuild.yaml

# List triggers
gcloud builds triggers list

# Run a trigger manually
gcloud builds triggers run TRIGGER_ID
```

## Identity and Access Management (IAM)

```bash
# List IAM policy
gcloud projects get-iam-policy PROJECT_ID

# Grant a role to a user
gcloud projects add-iam-policy-binding PROJECT_ID \
  --member=user:user@example.com \
  --role=roles/editor

# Grant a role to a service account
gcloud projects add-iam-policy-binding PROJECT_ID \
  --member=serviceAccount:sa@PROJECT-ID.iam.gserviceaccount.com \
  --role=roles/compute.admin

# Remove a role
gcloud projects remove-iam-policy-binding PROJECT_ID \
  --member=user:user@example.com \
  --role=roles/editor

# Create service account
gcloud iam service-accounts create my-sa \
  --display-name="My Service Account"

# List service accounts
gcloud iam service-accounts list

# Grant role to service account
gcloud projects add-iam-policy-binding PROJECT_ID \
  --member=serviceAccount:my-sa@PROJECT-ID.iam.gserviceaccount.com \
  --role=roles/storage.admin

# Create service account key
gcloud iam service-accounts keys create key.json \
  --iam-account=my-sa@PROJECT-ID.iam.gserviceaccount.com

# List service account keys
gcloud iam service-accounts keys list \
  --iam-account=my-sa@PROJECT-ID.iam.gserviceaccount.com

# Delete service account key
gcloud iam service-accounts keys delete KEY_ID \
  --iam-account=my-sa@PROJECT-ID.iam.gserviceaccount.com

# List custom roles
gcloud iam roles list --custom-only
```

## Cloud SQL

```bash
# Enable Cloud SQL API
gcloud services enable sqladmin.googleapis.com

# Create Cloud SQL instance (MySQL)
gcloud sql instances create my-instance \
  --database-version MYSQL_5_7 \
  --region us-central1 \
  --tier db-f1-micro

# Create Cloud SQL instance (PostgreSQL)
gcloud sql instances create my-instance \
  --database-version POSTGRES_13 \
  --region us-central1 \
  --tier db-f1-micro

# List instances
gcloud sql instances list

# Describe instance
gcloud sql instances describe my-instance

# Delete instance
gcloud sql instances delete my-instance

# Create database
gcloud sql databases create my-db \
  --instance=my-instance

# List databases
gcloud sql databases list --instance=my-instance

# Create user
gcloud sql users create my-user \
  --instance=my-instance \
  --password=PASSWORD

# Get connection string
gcloud sql instances describe my-instance \
  --format="value(ipAddresses[0].ipAddress)"
```

## Pub/Sub

```bash
# Enable Pub/Sub API
gcloud services enable pubsub.googleapis.com

# Create topic
gcloud pubsub topics create my-topic

# List topics
gcloud pubsub topics list

# Delete topic
gcloud pubsub topics delete my-topic

# Create subscription
gcloud pubsub subscriptions create my-sub \
  --topic=my-topic

# List subscriptions
gcloud pubsub subscriptions list

# Pull messages
gcloud pubsub subscriptions pull my-sub --auto-ack

# Publish message
gcloud pubsub topics publish my-topic \
  --message="Hello World"

# Delete subscription
gcloud pubsub subscriptions delete my-sub
```

## Services and APIs

```bash
# List available services
gcloud services list --available

# List enabled services
gcloud services list --enabled

# Enable an API
gcloud services enable compute.googleapis.com

# Disable an API
gcloud services disable compute.googleapis.com

# Check if service is enabled
gcloud services list --enabled \
  --filter="name:compute.googleapis.com"
```

## Networking

```bash
# Create VPC
gcloud compute networks create my-network

# List networks
gcloud compute networks list

# Create subnet
gcloud compute networks subnets create my-subnet \
  --network=my-network \
  --range=10.0.1.0/24 \
  --region=us-central1

# List subnets
gcloud compute networks subnets list

# Delete subnet
gcloud compute networks subnets delete my-subnet \
  --region=us-central1

# Create VPN gateway
gcloud compute vpn-gateways create my-vpn

# List VPN gateways
gcloud compute vpn-gateways list
```

## Key Management

```bash
# List KMS keyrings
gcloud kms keyrings list --location=us-central1

# Create KMS keyring
gcloud kms keyrings create my-keyring \
  --location=us-central1

# Create KMS key
gcloud kms keys create my-key \
  --location=us-central1 \
  --keyring=my-keyring \
  --purpose=encryption

# Encrypt data
gcloud kms encrypt \
  --location=us-central1 \
  --keyring=my-keyring \
  --key=my-key \
  --plaintext-file=file.txt \
  --ciphertext-file=file.txt.enc

# Decrypt data
gcloud kms decrypt \
  --location=us-central1 \
  --keyring=my-keyring \
  --key=my-key \
  --ciphertext-file=file.txt.enc \
  --plaintext-file=file.txt
```

## Monitoring and Logging

```bash
# Write log entry
gcloud logging write my-log "Message" --severity=INFO

# Read logs
gcloud logging read "resource.type=gce_instance" --limit=10

# Create log sink
gcloud logging sinks create my-sink \
  bigquery.googleapis.com/projects/PROJECT_ID/datasets/my_dataset \
  --log-filter='resource.type="gce_instance"'

# List log sinks
gcloud logging sinks list

# Delete log sink
gcloud logging sinks delete my-sink
```

## Tips and Tricks

```bash
# Output JSON format
gcloud compute instances list --format=json

# Filter results
gcloud compute instances list --filter="zone:us-central1-a"

# Get help on any command
gcloud compute instances create --help

# Set multiple configurations
gcloud config configurations create dev
gcloud config set project PROJECT_ID --configuration=dev

# Use flags instead of prompts
gcloud compute instances create my-instance --quiet
```

## Essential Tips

- **Always verify commands**: Use `--help` for detailed documentation
- **Use configuration profiles**: Manage multiple projects with configurations
- **Enable required APIs**: Many commands require API enablement first
- **Use default values**: Set commonly used zone/region to avoid flag repetition
- **Script your workflows**: Automate repetitive tasks with shell scripts
- **Save outputs**: Use `--format=json` for programmatic access to data
