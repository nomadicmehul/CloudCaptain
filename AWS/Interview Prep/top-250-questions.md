## TOP 250+ Interviews Questions on AWS

Q1) What is AWS?
Answer:AWS stands for Amazon Web Services. AWS is a platform that provides on-demand
resources for hosting web services, storage, networking, databases and other resources over the
internet with a pay-as-you-go pricing.

Q2) What are the components of AWS?
Answer:EC2 – Elastic Compute Cloud, S3 – Simple Storage Service, Route53, EBS – Elastic Block
Store, Cloudwatch, Key-Paris are few of the components of AWS.

Q3) What are key-pairs?
Answer:Key-pairs are secure login information for your instances/virtual machines. To connect to the
instances we use key-pairs that contain a public-key and private-key.

Q4) What is S3?
Answer:S3 stands for Simple Storage Service. It is a storage service that provides an interface that
you can use to store any amount of data, at any time, from anywhere in the world. With S3 you pay
only for what you use and the payment model is pay-as-you-go.

Q5) What are the pricing models for EC2instances?
Answer:The different pricing model for EC2 instances are as below,
* On-demand
* Reserved
* Spot
* Scheduled
* Dedicated

Q6) What are the types of volumes for EC2 instances?
Answer:
* There are two types of volumes,
* Instance store volumes
* EBS – Elastic Block Stores

Q7) What are EBS volumes?
Answer:EBS stands for Elastic Block Stores. They are persistent volumes that you can attach to the
instances. With EBS volumes, your data will be preserved even when you stop your instances, unlike
your instance store volumes where the data is deleted when you stop the instances.

Q8) What are the types of volumes in EBS?
Answer:Following are the types of volumes in EBS,
* General purpose
* Provisioned IOPS
* Magnetic
* Cold HDD
* Throughput optimized

Q9) What are the different types of instances?
Answer: Following are the types of instances,
* General purpose
* Computer Optimized
* Storage Optimized
* Memory Optimized
* Accelerated Computing

Q10) What is an auto-scaling and what are the components?
Answer: Auto scaling allows you to automatically scale-up and scale-down the number of instances
depending on the CPU utilization or memory utilization. There are 2 components in Auto scaling, they
are Auto-scaling groups and Launch Configuration.

Q11) What are reserved instances?
Answer: Reserved instances are the instance that you can reserve a fixed capacity of EC2 instances. In
reserved instances you will have to get into a contract of 1 year or 3 years.

Q12)What is an AMI?
Answer: AMI stands for Amazon Machine Image. AMI is a template that contains the software
configurations, launch permission and a block device mapping that specifies the volume to attach to
the instance when it is launched.

Q13) What is an EIP?
Answer: EIP stands for Elastic IP address. It is designed for dynamic cloud computing. When you
want to have a static IP address for your instances when you stop and restart your instances, you will
be using EIP address.

Q14) What is Cloudwatch?
Answer: Cloudwatch is a monitoring tool that you can use to monitor your various AWS resources.
Like health check, network, Application, etc.

Q15) What are the types in cloudwatch?
Answer: There are 2 types in cloudwatch. Basic monitoring and detailed monitoring. Basic monitoring
is free and detailed monitoring is chargeable.

Q16) What are the cloudwatch metrics that are available for EC2 instances?
Answer: Diskreads, Diskwrites, CPU utilization, networkpacketsIn, networkpacketsOut, networkIn,
networkOut, CPUCreditUsage, CPUCreditBalance.

Q17) What is the minimum and maximum size of individual objects that you can store in S3
Answer: The minimum size of individual objects that you can store in S3 is 0 bytes and the maximum
bytes that you can store for individual objects is 5TB.

Q18) What are the different storage classes in S3?
Answer: Following are the types of storage classes in S3,
* Standard frequently accessed
* Standard infrequently accessed * One-zone infrequently accessed.
* Glacier
* RRS – reduced redundancy storage

Q19) What is the default storage class in S3?
Answer: The default storage class in S3 in Standard frequently accessed.

Q20) What is glacier?
Answer: Glacier is the back up or archival tool that you use to back up your data in S3.

Q21) How can you secure the access to your S3 bucket?
Answer: There are two ways that you can control the access to your S3 buckets,
* ACL – Access Control List
* Bucket polices

Q22) How can you encrypt data in S3?
Answer: You can encrypt the data by using the below methods,
* Server Side Encryption – S3 (AES 256 encryption)
* Server Side Encryption – KMS (Key management Service)
* Server Side Encryption – C (Client Side)

Q23) What are the parameters for S3 pricing?
Answer: The pricing model for S3 is as below,
* Storage used
* Number of requests you make
* Storage management
* Data transfer
* Transfer acceleration

Q24) What is the pre-requisite to work with Cross region replication in S3?
Answer: You need to enable versioning on both source bucket and destination to work with cross
region replication. Also both the source and destination bucket should be in different region.

Q25) What are roles?
Answer: Roles are used to provide permissions to entities that you trust within your AWS account.
Roles are users in another account. Roles are similar to users but with roles you do not need to create
any username and password to work with the resources.

Q26) What are policies and what are the types of policies?
Answer: Policies are permissions that you can attach to the users that you create. These policies will
contain that access that you have provided to the users that you have created. There are 2 types of
policies.
* Managed policies
* Inline policies

Q27) What is cloudfront?
Answer: Cloudfront is an AWS web service that provided businesses and application developers an
easy and efficient way to distribute their content with low latency and high data transfer speeds.
Cloudfront is content delivery network of AWS.

Q28) What are edge locations?
Answer: Edge location is the place where the contents will be cached. When a user tries to access
some content, the content will be searched in the edge location. If it is not available then the content
will be made available from the origin location and a copy will be stored in the edge location.

Q29) What is the maximum individual archive that you can store in glacier?
Answer: You can store a maximum individual archive of upto 40 TB.

Q30) What is VPC?
Answer: VPC stands for Virtual Private Cloud. VPC allows you to easily customize your networking
configuration. VPC is a network that is logically isolated from other network in the cloud. It allows
you to have your own IP address range, subnets, internet gateways, NAT gateways and security
groups.

Q31) What is VPC peering connection?
Answer: VPC peering connection allows you to connect 1 VPC with another VPC. Instances in these
VPC behave as if they are in the same network.

Q32) What are NAT gateways?
Answer: NAT stands for Network Address Translation. NAT gateways enables instances in a private
subnet to connect to the internet but prevent the internet from initiating a connection with those
instances.

Q33) How can you control the security to your VPC?
Answer: You can use security groups and NACL (Network Access Control List) to control the
security to your
VPC.

Q34) What are the different types of storage gateway?
Answer: Following are the types of storage gateway.
* File gateway
* Volume gateway
* Tape gateway

Q35) What is a snowball?
Answer: Snowball is a data transport solution that used source appliances to transfer large amounts of
data into and out of AWS. Using snowball, you can move huge amount of data from one place to
another which reduces your network costs, long transfer times and also provides better security.

Q36) What are the database types in RDS?
Answer: Following are the types of databases in RDS,
* Aurora
* Oracle
* MYSQL server
* Postgresql
* MariaDB
* SQL server

Q37) What is a redshift?
Answer: Amazon redshift is a data warehouse product. It is a fast and powerful, fully managed,
petabyte scale data warehouse service in the cloud.

Q38) What is SNS?
Answer: SNS stands for Simple Notification Service. SNS is a web service that makes it easy to
notifications from the cloud. You can set up SNS to receive email notification or message notification.

Q39) What are the types of routing polices in route53?
Answer: Following are the types of routing policies in route53,
* Simple routing
* Latency routing
* Failover routing
* Geolocation routing
* Weighted routing
* Multivalue answer

Q40) What is the maximum size of messages in SQS?
Answer: The maximum size of messages in SQS is 256 KB.

Q41) What are the types of queues in SQS?
Answer: There are 2 types of queues in SQS.
* Standard queue
* FIFO (First In First Out)

Q42) What is multi-AZ RDS?
Answer: Multi-AZ (Availability Zone) RDS allows you to have a replica of your production database
in another availability zone. Multi-AZ (Availability Zone) database is used for disaster recovery. You
will have an exact copy of your database. So when your primary database goes down, your application
will automatically failover to the standby database.

Q43) What are the types of backups in RDS database?
Answer: There are 2 types of backups in RDS database.
* Automated backups
* Manual backups which are known as snapshots.

Q44) What is the difference between security groups and network access control list?
Answer:
Security Groups Network access control list
Can control the access at the instance level Can control access at the subnet level
Can add rules for “allow” only Can add rules for both “allow” and “deny”
Evaluates all rules before allowing the traffic Rules are processed in order number when
allowing traffic.
Can assign unlimited number of security groups Can assign upto 5 security groups.
Statefull filtering Stateless filtering

Q45) What are the types of load balancers in EC2?
Answer: There are 3 types of load balancers,
* Application load balancer
* Network load balancer
* Classic load balancer

Q46) What is and ELB?
Answer: ELB stands for Elastic Load balancing. ELB automatically distributes the incoming
application traffic or network traffic across multiple targets like EC2, containers, IP addresses.

Q47) What are the two types of access that you can provide when you are creating users?
Answer: Following are the two types of access that you can create.
* Programmatic access
* Console access

Q48) What are the benefits of auto scaling?
Answer: Following are the benefits of auto scaling
* Better fault tolerance
* Better availability
* Better cost management

Q49) What are security groups?
Answer: Security groups acts as a firewall that contains the traffic for one or more instances. You can
associate one or more security groups to your instances when you launch then. You can add rules to
each security group that allow traffic to and from its associated instances. You can modify the rules of
a security group at any time, the new rules are automatically and immediately applied to all the
instances that are associated with the security group

Q50) What are shared AMI’s?
Answer: Shared AMI’s are the AMI that are created by other developed and made available for other
developed to use.

Q51)What is the difference between the classic load balancer and application load balancer?
Answer: Dynamic port mapping, multiple port multiple listeners is used in Application Load
Balancer, One port one listener is achieved via Classic Load Balancer

Q52) By default how many Ip address does aws reserve in a subnet?
Answer: 5

Q53) What is meant by subnet?
Answer: A large section of IP Address divided in to chunks are known as subnets

Q54) How can you convert a public subnet to private subnet?
Answer: Remove IGW & add NAT Gateway, Associate subnet in Private route table

Q55) Is it possible to reduce a ebs volume?
Answer: no it’s not possible, we can increase it but not reduce them

Q56) What is the use of elastic ip are they charged by AWS?
Answer: These are ipv4 address which are used to connect the instance from internet, they are charged
if the instances are not attached to it

Q57) One of my s3 is bucket is deleted but i need to restore is there any possible way?
Answer: If versioning is enabled we can easily restore them

Q58) When I try to launch an ec2 instance i am getting Service limit exceed, how to fix the
issue?
Answer: By default AWS offer service limit of 20 running instances per region, to fix the issue we
need to contact AWS support to increase the limit based on the requirement

Q59) I need to modify the ebs volumes in Linux and windows is it possible
Answer: yes its possible from console use modify volumes in section give the size u need then for
windows go to disk management for Linux mount it to achieve the modification

Q60) Is it possible to stop a RDS instance, how can I do that?
Answer: Yes it’s possible to stop rds. Instance which are non-production and non multi AZ’s

Q61) What is meant by parameter groups in rds. And what is the use of it?
Answer: Since RDS is a managed service AWS offers a wide set of parameter in RDS as parameter
group which is modified as per requirement

Q62) What is the use of tags and how they are useful?
Answer: Tags are used for identification and grouping AWS Resources

Q63) I am viewing an AWS Console but unable to launch the instance, I receive an IAM Error
how can I rectify it?
Answer: As AWS user I don’t have access to use it, I need to have permissions to use it further

Q64) I don’t want my AWS Account id to be exposed to users how can I avoid it?
Answer: In IAM console there is option as sign in url where I can rename my own account name with
AWS account

Q65) By default how many Elastic Ip address does AWS Offer?
Answer: 5 elastic ip per region

Q66) You are enabled sticky session with ELB. What does it do with your instance?
Answer: Binds the user session with a specific instance

Q67) Which type of load balancer makes routing decisions at either the transport layer or the
Application layer and supports either EC2 or VPC.
Answer: Classic Load Balancer

Q68) Which is virtual network interface that you can attach to an instance in a VPC?
Answer: Elastic Network Interface

Q69) You have launched a Linux instance in AWS EC2. While configuring security group, you
Have selected SSH, HTTP, HTTPS protocol. Why do we need to select SSH?
Answer: To verify that there is a rule that allows traffic from EC2 Instance to your computer

Q70) You have chosen a windows instance with Classic and you want to make some change to
the
Security group. How will these changes be effective?
Answer: Changes are automatically applied to windows instances

Q71) Load Balancer and DNS service comes under which type of cloud service?
Answer: IAAS-Storage

Q72) You have an EC2 instance that has an unencrypted volume. You want to create another
Encrypted volume from this unencrypted volume. Which of the following steps can achieve
this?
Answer: Create a snapshot of the unencrypted volume (applying encryption parameters), copy the.
Snapshot and create a volume from the copied snapshot

Q73) Where does the user specify the maximum number of instances with the auto scaling
Commands?
Answer: Auto scaling Launch Config

Q74) Which are the types of AMI provided by AWS?
Answer: Instance Store backed, EBS Backed

Q75) After configuring ELB, you need to ensure that the user requests are always attached to a
Single instance. What setting can you use?
Answer: Sticky session

Q76) When do I prefer to Provisioned IOPS over the Standard RDS storage?
Answer:If you have do batch-oriented is workloads.

Q77) If I am running on my DB Instance a Multi-AZ deployments, can I use to the stand by the
DB Instance for read or write a operation along with to primary DB instance?
Answer: Primary db instance does not working.

Q78) Which the AWS services will you use to the collect and the process e-commerce data for
the near by real-time analysis?
Answer: Good of Amazon DynamoDB.

Q79) A company is deploying the new two-tier an web application in AWS. The company has to
limited on staff and the requires high availability, and the application requires to complex queries
and table joins. Which configuration provides to the solution for company’s requirements?
Answer: An web application provide on Amazon DynamoDB solution.

Q80) Which the statement use to cases are suitable for Amazon DynamoDB?
Answer:The storing metadata for the Amazon S3 objects& The Running of relational joins and
complex an updates.

Q81) Your application has to the retrieve on data from your user’s mobile take every 5 minutes
and then data is stored in the DynamoDB, later every day at the particular time the data is an
extracted into S3 on a per user basis and then your application is later on used to visualize the
data to user. You are the asked to the optimize the architecture of the backend system can to
lower cost, what would you recommend do?
Answer: Introduce Amazon Elasticache to the cache reads from the Amazon DynamoDB table and to
reduce the provisioned read throughput.

Q82) You are running to website on EC2 instances can deployed across multiple Availability
Zones with an Multi-AZ RDS MySQL Extra Large DB Instance etc. Then site performs a high
number of the small reads and the write per second and the relies on the eventual consistency
model. After the comprehensive tests you discover to that there is read contention on RDS
MySQL. Which is the best approaches to the meet these requirements?
Answer:The Deploy Elasti Cache in-memory cache is running in each availability zone and Then
Increase the RDS MySQL Instance size and the Implement provisioned IOPS.

Q83) An startup is running to a pilot deployment of around 100 sensors to the measure street
noise and The air quality is urban areas for the 3 months. It was noted that every month to
around the 4GB of sensor data are generated. The company uses to a load balanced take auto
scaled layer of the EC2 instances and a RDS database with a 500 GB standard storage. The pilot
was success and now they want to the deploy take atleast 100K sensors.let which to need the
supported by backend. You need to the stored data for at least 2 years to an analyze it. Which
setup of following would you be prefer?
Answer: The Replace the RDS instance with an 6 node Redshift cluster with take 96TB of storage.

Q84) Let to Suppose you have an application where do you have to render images and also do
some of general computing. which service will be best fit your need?
Answer:Used on Application Load Balancer.