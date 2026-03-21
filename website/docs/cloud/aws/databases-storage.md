---
title: "AWS Databases & Storage"
sidebar_label: "Databases & Storage"
description: "AWS database and storage services — S3, EBS, RDS, DynamoDB, Aurora, Redshift, and data management best practices"
sidebar_position: 5
---

# AWS Databases & Storage

AWS provides a comprehensive suite of database and storage services designed for different use cases, from high-performance relational databases to scalable NoSQL solutions and petabyte-scale data warehouses. This guide covers the most commonly used services and best practices for selecting and managing them.

## S3 (Simple Storage Service)

S3 is AWS's object storage service, designed for durability, availability, and scalability. It's the foundation for many AWS architectures.

### Core Concepts

- **Buckets**: Top-level containers for objects (like directories)
- **Objects**: Files stored in S3, consisting of data and metadata
- **Keys**: Unique identifiers for objects within a bucket (full path)
- **Versioning**: Enable to maintain multiple versions of objects for protection against accidental deletion

### Storage Classes Comparison

| Storage Class | Min Storage | Access Time | Cost per GB | Best For |
|---|---|---|---|---|
| **Standard** | None | Immediate | Highest | Frequent access, active data |
| **Standard-IA** | 30 days | Immediate | Lower | Infrequent access (1 month+) |
| **One Zone-IA** | 30 days | Immediate | Lowest warm | Infrequent, non-critical data |
| **Intelligent-Tiering** | None | Immediate | Variable | Unknown access patterns |
| **Glacier Instant** | 90 days | Minutes | Low | Archive with quick retrieval |
| **Glacier Flexible** | 90 days | 3-5 hours | Lower | Archive, predictable retrieval |
| **Glacier Deep Archive** | 180 days | 12 hours | Lowest | Long-term archive |

### Lifecycle Policies

Automatically transition objects between storage classes or delete them based on age:

```json
{
  "Rules": [
    {
      "Id": "ArchiveAfter90Days",
      "Status": "Enabled",
      "Prefix": "logs/",
      "Transitions": [
        {
          "Days": 30,
          "StorageClass": "STANDARD_IA"
        },
        {
          "Days": 90,
          "StorageClass": "GLACIER_IR"
        }
      ],
      "Expiration": {
        "Days": 365
      },
      "NoncurrentVersionTransitions": [
        {
          "NoncurrentDays": 7,
          "StorageClass": "GLACIER_FLEXIBLE"
        }
      ]
    }
  ]
}
```

### Encryption

- **SSE-S3**: Server-side encryption with S3-managed keys (default)
- **SSE-KMS**: Server-side encryption with AWS KMS (customer control, auditable)
- **SSE-C**: Server-side encryption with customer-provided keys
- **Client-side**: Encrypt before uploading (CloudFront, SDK)

### Access Control

- **Bucket Policies**: JSON policies attached to buckets (recommended)
- **ACLs**: Object and bucket-level access (legacy, not recommended)
- **Access Points**: Simplified access management for applications
- **Block Public Access**: Four settings to prevent accidental public exposure

### S3 Static Website Hosting

```bash
# Enable static website hosting
aws s3 website s3://mybucket --index-document index.html --error-document error.html

# Upload files
aws s3 cp . s3://mybucket --recursive --exclude ".git/*"
```

### Advanced Features

- **Cross-Region Replication (CRR)**: Automatically replicate objects to another region
- **S3 Transfer Acceleration**: Use CloudFront edge locations for faster uploads
- **Event Notifications**: Trigger Lambda, SNS, or SQS on object changes
- **S3 Batch Operations**: Apply actions to millions of objects at scale

### AWS CLI Examples

```bash
# Create bucket
aws s3 mb s3://my-unique-bucket --region us-east-1

# Upload file
aws s3 cp myfile.txt s3://my-bucket/

# Upload directory
aws s3 sync ./local-dir s3://my-bucket/remote-dir --delete

# List objects
aws s3 ls s3://my-bucket/ --recursive

# Set lifecycle policy
aws s3api put-bucket-lifecycle-configuration \
  --bucket my-bucket \
  --lifecycle-configuration file://lifecycle.json

# Enable versioning
aws s3api put-bucket-versioning \
  --bucket my-bucket \
  --versioning-configuration Status=Enabled

# Block public access
aws s3api put-public-access-block \
  --bucket my-bucket \
  --public-access-block-configuration \
  "BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true"
```

---

## EBS (Elastic Block Store)

EBS provides block-level storage volumes for EC2 instances. Choose the right volume type based on performance and cost requirements.

### Volume Types Comparison

| Type | IOPS | Throughput | Use Case |
|---|---|---|---|
| **gp3** | 3,000-16,000 | 125-1,000 MB/s | General purpose, web apps, databases |
| **gp2** | 100-16,000 | 40-250 MB/s | Legacy general purpose (use gp3) |
| **io2** | 64,000-256,000 | 1,000-4,000 MB/s | High-performance databases, NoSQL |
| **io1** | 100-64,000 | 500-1,000 MB/s | Legacy high-IOPS (use io2) |
| **st1** | 500 | 500 MB/s | Throughput-optimized (Hadoop, logs) |
| **sc1** | 250 | 250 MB/s | Cold storage, infrequent access |

### Key Features

- **Snapshots**: Point-in-time backups (incremental, stored in S3)
- **Encryption**: At-rest encryption with AWS KMS
- **Multi-Attach**: Attach single io1/io2 volume to multiple instances (shared storage)
- **Fast Snapshot Restore**: Create volumes from snapshots with full performance immediately
- **EBS-Optimized**: Instances with dedicated EBS bandwidth

### EBS vs Instance Store

| Feature | EBS | Instance Store |
|---|---|---|
| **Persistence** | Survives instance termination | Lost on termination |
| **Performance** | Network-attached | Local, higher throughput |
| **Cost** | Pay per GB stored | No additional cost |
| **Use Case** | Databases, file systems | Temporary, cache |

### AWS CLI Examples

```bash
# Create volume (100 GB gp3)
aws ec2 create-volume \
  --size 100 \
  --volume-type gp3 \
  --iops 3000 \
  --throughput 125 \
  --availability-zone us-east-1a

# Create snapshot
aws ec2 create-snapshot --volume-id vol-12345 --description "DB backup"

# Create volume from snapshot
aws ec2 create-volume \
  --snapshot-id snap-12345 \
  --availability-zone us-east-1a

# Attach volume to instance
aws ec2 attach-volume \
  --volume-id vol-12345 \
  --instance-id i-12345 \
  --device /dev/sdf
```

---

## EFS (Elastic File System)

EFS provides managed NFS (Network File System) storage for EC2 instances, supporting thousands of concurrent connections.

### Key Features

- **Shared Access**: Multiple instances mount the same file system
- **Performance Modes**:
  - **General Purpose** (default): Lowest latency, suitable for most workloads
  - **Max I/O**: Higher levels of throughput and operations per second
- **Storage Classes**:
  - **Standard**: Frequently accessed files
  - **Infrequent Access (IA)**: Automatic transition to lower cost for rarely used files
- **Encryption**: At-rest with KMS, in-transit with TLS
- **Lifecycle Policies**: Automatic transition to IA after 30 days (configurable)

### Comparison: EFS vs EBS vs S3

| Feature | EFS | EBS | S3 |
|---|---|---|---|
| **Access** | Multiple instances | Single instance | Unlimited |
| **Latency** | Sub-millisecond | Microsecond | Milliseconds |
| **Protocol** | NFS | Block | HTTP/S |
| **Cost** | Per GB stored | Per GB stored | Per GB stored |
| **Use Case** | Shared file systems | Instance volumes | Object storage |

---

## RDS (Relational Database Service)

AWS-managed relational database service eliminating administrative overhead while providing high availability and scalability.

### Supported Engines

- **MySQL**: Open-source RDBMS
- **PostgreSQL**: Advanced open-source RDBMS with JSON, UUID support
- **MariaDB**: MySQL fork with improved performance
- **Oracle Database**: Enterprise RDBMS
- **SQL Server**: Microsoft enterprise database
- **Aurora**: AWS-native MySQL and PostgreSQL compatible (covered separately)

### High Availability & Reliability

- **Multi-AZ**: Synchronous standby replica in different AZ with automatic failover (RTO: 1-2 minutes)
- **Read Replicas**: Asynchronous replicas for read scaling (same region or cross-region)
- **Automated Backups**: Daily snapshots retained for 7-35 days
- **Manual Snapshots**: User-initiated backups retained indefinitely
- **Backup Window**: Specify preferred time for automated backups

### Performance & Scaling

- **Instance Classes**: db.t3.micro to db.x1e.32xlarge (compute-optimized, memory-optimized)
- **Storage Auto-Scaling**: Automatically expand storage as needed
- **RDS Proxy**: Connection pooling and IAM authentication
  - Reduces database connections by 90%
  - Improves application resilience
  - Enables seamless failover

### Parameter Groups & Option Groups

- **Parameter Group**: Database engine configuration (max connections, charset, timeout)
- **Option Group**: Add-ons like backup, replication, encryption

### AWS CLI Examples

```bash
# Create MySQL instance with Multi-AZ
aws rds create-db-instance \
  --db-instance-identifier mydb \
  --db-instance-class db.t3.micro \
  --engine mysql \
  --master-username admin \
  --master-user-password MyPassword123 \
  --allocated-storage 100 \
  --multi-az \
  --backup-retention-period 7

# Create read replica
aws rds create-db-instance-read-replica \
  --db-instance-identifier mydb-replica \
  --source-db-instance-identifier mydb

# Create snapshot
aws rds create-db-snapshot \
  --db-snapshot-identifier mydb-snapshot \
  --db-instance-identifier mydb

# Restore from snapshot
aws rds restore-db-instance-from-db-snapshot \
  --db-instance-identifier mydb-restored \
  --db-snapshot-identifier mydb-snapshot

# Describe instances
aws rds describe-db-instances --query 'DBInstances[*].[DBInstanceIdentifier,DBInstanceStatus]'
```

---

## Amazon Aurora

AWS-native relational database designed for cloud with 5x MySQL and 3x PostgreSQL performance, combined with high availability and scalability.

### Architecture

- **Shared Storage**: Cluster volume across multiple AZs (auto-replicating, self-healing)
- **Compute**: Multiple instance types for different performance tiers
- **Replicas**: Up to 15 read replicas with automatic failover
- **Performance**: Optimized for throughput and low latency

### Aurora Serverless v2

- **Auto-scaling**: Automatically adjust capacity based on demand
- **Per-second Billing**: Pay for what you use, not reserved capacity
- **Cold Start**: Starts in seconds with warm pools
- **Ideal For**: Unpredictable workloads, development/testing, variable traffic

### Advanced Features

- **Aurora Global Database**: Read-only replicas in other regions for DR and low-latency reads
- **Aurora Backtrack**: Rewind database to any point in last 72 hours (no restore needed)
- **Parallel Query**: Distributed queries across cluster for fast scans
- **Smart Driver**: Connection awareness and read/write routing

### Aurora vs RDS Comparison

| Feature | Aurora | RDS (MySQL/PostgreSQL) |
|---|---|---|
| **Performance** | 5x/3x faster | Standard |
| **Availability** | Built-in with 15 replicas | Multi-AZ standby only |
| **Cost** | Higher compute | Lower compute |
| **Scaling** | Automatic cluster scaling | Manual or Auto-Scaling |
| **Recovery** | Backtrack | Snapshots only |

### AWS CLI Examples

```bash
# Create Aurora MySQL cluster (serverless v2)
aws rds create-db-cluster \
  --db-cluster-identifier aurora-cluster \
  --engine aurora-mysql \
  --engine-version 8.0.mysql_aurora.3.04.0 \
  --master-username admin \
  --master-user-password MyPassword123

# Add instance to cluster
aws rds create-db-instance \
  --db-instance-identifier aurora-primary \
  --db-instance-class db.serverless \
  --engine aurora-mysql \
  --db-cluster-identifier aurora-cluster

# Create read replica
aws rds create-db-instance \
  --db-instance-identifier aurora-replica \
  --db-instance-class db.t3.small \
  --engine aurora-mysql \
  --db-cluster-identifier aurora-cluster
```

---

## DynamoDB

Fully managed NoSQL database supporting key-value and document data models with automatic scaling and millisecond latency.

### Core Concepts

- **Table**: Collection of items
- **Item**: Similar to a row (max 400 KB)
- **Attribute**: Similar to a column (no fixed schema)
- **Primary Key**: Uniquely identifies an item
  - **Partition Key**: Hash key for distribution across partitions
  - **Sort Key**: Optional, enables range queries (partition key + sort key = composite key)

### Capacity Modes

- **Provisioned**: Reserve read/write capacity units (RCU/WCU)
  - RCU: 4 KB read per second
  - WCU: 1 KB write per second
  - Auto-scaling available
- **On-Demand**: Pay per request, suitable for unpredictable workloads
  - No capacity planning
  - Higher per-request cost

### Secondary Indexes

- **Global Secondary Index (GSI)**:
  - Different partition/sort key from base table
  - Eventual consistency
  - Separate provisioned capacity
  - Can be added/removed after creation
- **Local Secondary Index (LSI)**:
  - Same partition key, different sort key
  - Strong consistency
  - 10 GB size limit
  - Must be created at table creation

### Advanced Features

- **DynamoDB Streams**: Capture item-level modifications (INSERT, UPDATE, DELETE)
  - Useful for Lambda triggers, CDC, replication
  - 24-hour retention
- **DAX (DynamoDB Accelerator)**: In-memory cache for microsecond latency
- **Global Tables**: Multi-region, fully replicated tables with eventual consistency
- **TTL**: Automatic expiration of items based on timestamp attribute

### Design Patterns

- **Sparse Indexes**: Some items have sort key, others don't (flexible schema)
- **Composite Sort Keys**: JSON-like sorting for complex queries
- **Batch Operations**: Use batch-get-item, batch-write-item for efficiency

### AWS CLI Examples

```bash
# Create table
aws dynamodb create-table \
  --table-name Users \
  --attribute-definitions \
    AttributeName=UserId,AttributeType=S \
    AttributeName=CreatedAt,AttributeType=N \
  --key-schema \
    AttributeName=UserId,KeyType=HASH \
    AttributeName=CreatedAt,KeyType=RANGE \
  --billing-mode PAY_PER_REQUEST

# Put item
aws dynamodb put-item \
  --table-name Users \
  --item '{"UserId": {"S": "user123"}, "Name": {"S": "John Doe"}, "Email": {"S": "john@example.com"}}'

# Get item
aws dynamodb get-item \
  --table-name Users \
  --key '{"UserId": {"S": "user123"}}'

# Query items by partition key
aws dynamodb query \
  --table-name Users \
  --key-condition-expression "UserId = :uid" \
  --expression-attribute-values '{":uid": {"S": "user123"}}'

# Scan table (full table scan - use with caution)
aws dynamodb scan --table-name Users --limit 10

# Update item
aws dynamodb update-item \
  --table-name Users \
  --key '{"UserId": {"S": "user123"}}' \
  --update-expression "SET #n = :val" \
  --expression-attribute-names '{"#n": "Name"}' \
  --expression-attribute-values '{":val": {"S": "Jane Doe"}}'

# Create Global Secondary Index
aws dynamodb update-table \
  --table-name Users \
  --attribute-definitions AttributeName=Email,AttributeType=S \
  --global-secondary-index-updates '[{"Create": {"IndexName": "EmailIndex", "Keys": [{"AttributeName": "Email", "KeyType": "HASH"}], "Projection": {"ProjectionType": "ALL"}, "ProvisionedThroughput": {"ReadCapacityUnits": 5, "WriteCapacityUnits": 5}}}]'

# Enable DynamoDB Streams
aws dynamodb update-table \
  --table-name Users \
  --stream-specification StreamEnabled=true,StreamViewType=NEW_AND_OLD_IMAGES

# Set TTL
aws dynamodb update-time-to-live \
  --table-name Users \
  --time-to-live-specification AttributeName=ExpiresAt,Enabled=true
```

---

## Amazon Redshift

Fully managed data warehouse service using columnar storage and massively parallel processing (MPP) for analytics at scale.

### Architecture

- **Columnar Storage**: Compress similar data types for faster queries
- **MPP**: Distribute queries across multiple nodes in parallel
- **Leader Node**: Coordinates query execution (smallest cluster size: 2 nodes)
- **Compute Nodes**: Perform calculations (dc2.large to ra3.4xlplus)

### Key Features

- **Redshift Spectrum**: Query data directly in S3 without loading into Redshift
- **Redshift Serverless**: Auto-scaling data warehouse without cluster management
- **Query Editor**: Web-based SQL editor with sample datasets
- **Concurrency Scaling**: Automatically add capacity for peak loads
- **Automated Backups**: Snapshots to S3 with 1-35 day retention

### Use Cases

- Historical analysis
- Trend detection
- Large-scale reporting
- Ad hoc queries on structured data

### AWS CLI Examples

```bash
# Create cluster
aws redshift create-cluster \
  --cluster-identifier my-cluster \
  --node-type dc2.large \
  --number-of-nodes 2 \
  --master-username awsuser \
  --master-user-password MyPassword123 \
  --db-name mydb \
  --publicly-accessible

# Create snapshot
aws redshift create-cluster-snapshot \
  --snapshot-identifier my-snapshot \
  --cluster-identifier my-cluster

# Restore from snapshot
aws redshift restore-from-cluster-snapshot \
  --cluster-identifier my-cluster-restored \
  --snapshot-identifier my-snapshot

# Query Redshift Spectrum (S3 data)
# First, create external schema and table pointing to S3
# Then query as normal SQL

# Example query (executed via Query Editor or psql)
# SELECT * FROM s3_data.my_table WHERE year = 2024;
```

---

## ElastiCache

Fully managed in-memory data store (Redis or Memcached) for caching and session management with microsecond latency.

### Redis vs Memcached

| Feature | Redis | Memcached |
|---|---|---|
| **Data Types** | Strings, lists, sets, hashes, sorted sets, streams | Strings only |
| **Persistence** | Optional (RDB snapshots, AOF logs) | None (volatile) |
| **Replication** | Primary/replica clusters | None |
| **Transactions** | ACID transactions | None |
| **Pub/Sub** | Supported | Not supported |
| **Lua Scripting** | Supported | Not supported |
| **Cluster** | Shard-based | Memcached Cluster Mode |
| **Use Case** | Complex caching, sessions, leaderboards, queues | Simple high-throughput caching |

### Caching Strategies

- **Lazy Loading (Cache-Aside)**:
  - Application checks cache first
  - On miss, fetch from database and populate cache
  - Simple, but slower on miss
  ```
  GET key
  IF result is NULL
    result = DB.query(key)
    CACHE.set(key, result, TTL=3600)
  RETURN result
  ```

- **Write-Through**:
  - Write to cache AND database simultaneously
  - Ensures cache is always up-to-date
  - Higher write latency
  ```
  CACHE.set(key, value)
  DB.write(key, value)
  ```

### Cluster Modes

- **Disabled**: Single primary node with optional replicas (no sharding)
- **Enabled**: Multiple shards, each with primary and replicas (horizontal scaling)

### AWS CLI Examples

```bash
# Create Redis cluster
aws elasticache create-cache-cluster \
  --cache-cluster-id my-redis \
  --cache-node-type cache.t3.micro \
  --engine redis \
  --num-cache-nodes 1

# Create Redis cluster with replicas and Multi-AZ
aws elasticache create-replication-group \
  --replication-group-description "Multi-AZ Redis" \
  --replication-group-id my-redis-group \
  --engine redis \
  --cache-node-type cache.t3.small \
  --num-cache-clusters 2 \
  --automatic-failover-enabled

# Create Memcached cluster
aws elasticache create-cache-cluster \
  --cache-cluster-id my-memcached \
  --cache-node-type cache.t3.micro \
  --engine memcached \
  --num-cache-nodes 3

# Describe cluster
aws elasticache describe-cache-clusters --cache-cluster-id my-redis

# Create parameter group (for custom Redis settings)
aws elasticache create-cache-parameter-group \
  --cache-parameter-group-name my-params \
  --cache-parameter-group-family redis7 \
  --description "Custom Redis parameters"

# Modify parameter
aws elasticache modify-cache-parameter-group \
  --cache-parameter-group-name my-params \
  --parameter-name maxmemory-policy \
  --parameter-value allkeys-lru
```

---

## Database Decision Guide

Choosing the right database is critical for application performance and cost. Use this guide to select the appropriate AWS service:

```
START: What is your primary data structure?
├─ Relational data (tables, joins, SQL)
│  ├─ Large-scale analytics queries? → REDSHIFT
│  ├─ Need 5x performance & auto-scaling? → AURORA
│  └─ Standard RDBMS? → RDS (MySQL/PostgreSQL)
│
├─ Key-value or document data
│  ├─ Unpredictable traffic? → DYNAMODB (On-Demand)
│  ├─ Predictable, high-volume? → DYNAMODB (Provisioned)
│  ├─ Simple caching? → ELASTICACHE (Memcached)
│  └─ Complex caching & sessions? → ELASTICACHE (Redis)
│
├─ Time-series data
│  ├─ Metrics & monitoring → TIMESTREAM
│  └─ Application logs → CLOUDWATCH LOGS
│
├─ Graph data (social networks, recommendations)
│  └─ NEPTUNE
│
├─ Immutable audit trail
│  └─ QLDB (Quantum Ledger Database)
│
├─ Search and analytics on documents
│  └─ OPENSEARCH
│
└─ Unstructured data (files, media)
   └─ S3
```

### Quick Selection Matrix

| Use Case | Best Service | Alternative |
|---|---|---|
| **Web application data** | RDS or Aurora | DynamoDB |
| **Mobile app backend** | DynamoDB | Firebase Realtime DB |
| **Session storage** | ElastiCache Redis | DynamoDB with TTL |
| **Leaderboards/rankings** | ElastiCache Redis (sorted sets) | DynamoDB with GSI |
| **Full-text search** | OpenSearch | DynamoDB with Elasticsearch |
| **Analytics queries** | Redshift | Athena (S3) |
| **Cache layer** | ElastiCache | CloudFront |
| **Data lake** | S3 + Athena + Glue | Redshift Spectrum |
| **Real-time streaming** | Kinesis | Lambda + DynamoDB |
| **Graph relationships** | Neptune | DynamoDB with adjacency lists |

---

## Data Management Best Practices

### Backup & Disaster Recovery

- **Regular Snapshots**: Automate backups for all databases
- **Cross-Region Replicas**: Protect against regional failures
- **Test Restores**: Periodically restore from backups to verify integrity
- **Retention Policy**: Balance storage costs with compliance requirements

### Performance Optimization

- **Connection Pooling**: Use RDS Proxy for databases, DAX for DynamoDB
- **Caching Strategy**: Implement multi-layer caching (ElastiCache → Database)
- **Indexing**: Create indexes on frequently queried columns
- **Query Optimization**: Use EXPLAIN to analyze slow queries
- **Sharding/Partitioning**: Distribute data for horizontal scaling

### Security

- **Encryption**: Enable at-rest and in-transit encryption
- **IAM Policies**: Use least-privilege access
- **Secrets Management**: Store credentials in AWS Secrets Manager
- **Network Isolation**: Use VPC security groups and subnets
- **Audit Logging**: Enable CloudTrail and database audit logs

### Cost Optimization

- **Right-Sizing**: Monitor utilization and adjust instance types
- **Storage Classes**: Use S3 lifecycle policies for cost reduction
- **Reserved Instances**: Commit to long-term capacity for discounts
- **On-Demand for Variables**: Use DynamoDB On-Demand for unpredictable workloads
- **Monitoring**: Set up CloudWatch alarms for anomalies

---

## Hands-On Exercises

### Exercise 1: Create S3 Bucket with Lifecycle Policy and Versioning

```bash
# Create bucket
aws s3 mb s3://my-lifecycle-bucket-$(date +%s)

# Enable versioning
aws s3api put-bucket-versioning \
  --bucket my-lifecycle-bucket-1234567890 \
  --versioning-configuration Status=Enabled

# Create lifecycle policy file (lifecycle.json)
cat > lifecycle.json << 'EOF'
{
  "Rules": [
    {
      "Id": "TransitionRule",
      "Status": "Enabled",
      "Prefix": "logs/",
      "Transitions": [
        {
          "Days": 30,
          "StorageClass": "STANDARD_IA"
        },
        {
          "Days": 90,
          "StorageClass": "GLACIER_IR"
        }
      ],
      "Expiration": {
        "Days": 365
      }
    }
  ]
}
EOF

# Apply lifecycle policy
aws s3api put-bucket-lifecycle-configuration \
  --bucket my-lifecycle-bucket-1234567890 \
  --lifecycle-configuration file://lifecycle.json

# Upload test files
echo "Test log 1" > logs/app-2024-01.log
echo "Test log 2" > logs/app-2024-02.log
aws s3 cp logs/ s3://my-lifecycle-bucket-1234567890/logs/ --recursive

# Verify versioning
aws s3api list-object-versions --bucket my-lifecycle-bucket-1234567890
```

### Exercise 2: Launch RDS MySQL with Multi-AZ and Read Replica

```bash
# Create MySQL instance with Multi-AZ
aws rds create-db-instance \
  --db-instance-identifier myapp-db \
  --db-instance-class db.t3.micro \
  --engine mysql \
  --engine-version 8.0.35 \
  --master-username admin \
  --master-user-password MySecurePassword123! \
  --allocated-storage 20 \
  --multi-az \
  --backup-retention-period 7 \
  --storage-encrypted \
  --enable-cloudwatch-logs-exports error,general,slowquery

# Wait for DB to be available (check status)
aws rds describe-db-instances \
  --db-instance-identifier myapp-db \
  --query 'DBInstances[0].DBInstanceStatus'

# Create read replica in same region
aws rds create-db-instance-read-replica \
  --db-instance-identifier myapp-db-replica \
  --source-db-instance-identifier myapp-db

# Verify replication lag
aws rds describe-db-instances \
  --db-instance-identifier myapp-db-replica \
  --query 'DBInstances[0].[DBInstanceStatus,ReplicationLag]'

# Connect to primary (get endpoint)
ENDPOINT=$(aws rds describe-db-instances \
  --db-instance-identifier myapp-db \
  --query 'DBInstances[0].Endpoint.Address' \
  --output text)

echo "Connect to primary: mysql -h $ENDPOINT -u admin -p"
echo "Connect to replica: mysql -h $(aws rds describe-db-instances --db-instance-identifier myapp-db-replica --query 'DBInstances[0].Endpoint.Address' --output text) -u admin -p"
```

### Exercise 3: Create DynamoDB Table and Query with CLI

```bash
# Create Users table
aws dynamodb create-table \
  --table-name Users \
  --attribute-definitions \
    AttributeName=UserId,AttributeType=S \
    AttributeName=CreatedAt,AttributeType=N \
  --key-schema \
    AttributeName=UserId,KeyType=HASH \
    AttributeName=CreatedAt,KeyType=RANGE \
  --billing-mode PAY_PER_REQUEST

# Wait for table creation
aws dynamodb wait table-exists --table-name Users

# Add sample items
aws dynamodb put-item \
  --table-name Users \
  --item '{
    "UserId": {"S": "user001"},
    "CreatedAt": {"N": "1694894400"},
    "Name": {"S": "Alice"},
    "Email": {"S": "alice@example.com"}
  }'

aws dynamodb put-item \
  --table-name Users \
  --item '{
    "UserId": {"S": "user002"},
    "CreatedAt": {"N": "1694894500"},
    "Name": {"S": "Bob"},
    "Email": {"S": "bob@example.com"}
  }'

# Query user by ID
aws dynamodb query \
  --table-name Users \
  --key-condition-expression "UserId = :uid" \
  --expression-attribute-values '{":uid": {"S": "user001"}}'

# Scan table (get all items)
aws dynamodb scan --table-name Users

# Create Global Secondary Index on Email
aws dynamodb update-table \
  --table-name Users \
  --attribute-definitions AttributeName=Email,AttributeType=S \
  --global-secondary-index-updates '[{
    "Create": {
      "IndexName": "EmailIndex",
      "Keys": [{"AttributeName": "Email", "KeyType": "HASH"}],
      "Projection": {"ProjectionType": "ALL"},
      "ProvisionedThroughput": {"ReadCapacityUnits": 5, "WriteCapacityUnits": 5}
    }
  }]'

# Query by email using GSI
aws dynamodb query \
  --table-name Users \
  --index-name EmailIndex \
  --key-condition-expression "Email = :email" \
  --expression-attribute-values '{":email": {"S": "alice@example.com"}}'
```

### Exercise 4: Set Up Redshift Cluster and Run Analytics Query

```bash
# Create Redshift cluster (note: this takes 10-15 minutes)
aws redshift create-cluster \
  --cluster-identifier analytics-cluster \
  --node-type dc2.large \
  --number-of-nodes 2 \
  --master-username awsuser \
  --master-user-password MyPassword123 \
  --db-name analytics \
  --publicly-accessible

# Wait for cluster availability
aws redshift describe-clusters \
  --cluster-identifier analytics-cluster \
  --query 'Clusters[0].ClusterStatus'

# Get cluster endpoint
ENDPOINT=$(aws redshift describe-clusters \
  --cluster-identifier analytics-cluster \
  --query 'Clusters[0].Endpoint.Address' \
  --output text)

echo "Cluster endpoint: $ENDPOINT"

# Connect using psql (install if needed: brew install libpq)
# psql -h $ENDPOINT -U awsuser -d analytics

# Create sample table and load data
cat > redshift-setup.sql << 'EOF'
CREATE TABLE sales (
  sale_id INT PRIMARY KEY,
  date DATE,
  product_name VARCHAR(100),
  quantity INT,
  price DECIMAL(10,2)
);

INSERT INTO sales VALUES
  (1, '2024-01-01', 'Widget A', 100, 29.99),
  (2, '2024-01-02', 'Widget B', 50, 49.99),
  (3, '2024-01-03', 'Widget A', 75, 29.99);

SELECT product_name, SUM(quantity) as total_sold, SUM(quantity * price) as total_revenue
FROM sales
GROUP BY product_name
ORDER BY total_revenue DESC;
EOF

# Execute script (requires psql installed and network access)
# psql -h $ENDPOINT -U awsuser -d analytics -f redshift-setup.sql
```

---

## Resources

- [AWS S3 Documentation](https://docs.aws.amazon.com/s3/)
- [AWS RDS Documentation](https://docs.aws.amazon.com/rds/)
- [AWS DynamoDB Documentation](https://docs.aws.amazon.com/dynamodb/)
- [AWS Aurora Documentation](https://docs.aws.amazon.com/rds/latest/AuroraUserGuide/)
- [AWS Redshift Documentation](https://docs.aws.amazon.com/redshift/)
- [AWS ElastiCache Documentation](https://docs.aws.amazon.com/elasticache/)
- [AWS EBS Documentation](https://docs.aws.amazon.com/ebs/)
- [AWS Database Migration Service](https://docs.aws.amazon.com/dms/)
