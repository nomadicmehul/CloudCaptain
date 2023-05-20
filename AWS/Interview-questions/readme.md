##### WHAT IS LOAD BALANCER?
Load balancer serves as the single point of content for clients and distributes incoming application traffic across multiple targets, such as EC2 instances in multiple availability zones. This increases the availability of your application. You add one or more listeners to your load balancer.

##### WHAT IS AUTO SCALING?
AWS Auto scaling monitors your applications and automatically adjusts capacity to maintain steady, predictable performance at the lowest possible cost. Using AWS auto scaling, it's easy to setup application scaling for multiple resources across multiple services in minutes.
"PAY ONLY FOR WHAT YOU NEED"
AWS auto scaling can help you optimize your utilization and cost efficiencies when consuming AWS devices, so you only pay for the resources you actually need. When demand drops, AWS auto scaling will automatically remove any excess resource capacity so you avoid over spending. AWS auto scaling is free to use, and allows you to optimize the costs of your AWS environment.

##### VPCs AND SUBNETS
A "subnet" defines a range of ip addresses in your VPC
You can launch AWS resources into a subnet that you select.
A "private subnet" should be used for resources that won't be accessible over the internet.
A "public subnet" should be used for resources that will be accessible over the internet.
Each subnet must be reside entirely within one availability zone and cannot span zones.

##### SECURITY GROUPS
1.Security groups are virtual firewalls that control inbound and outbound traffic for one or more instances.
2. Security groups deny all incoming traffic by default and use allow rules that can filter based on TCP, UDP and ICMP protocols.
3. Security groups are stateful which means that if your inbound request is allowed, the outbound response does not have to be inspected/tracked and vice versa.
4. Security groups can define a source/target as either a CIDR block or another security group to handle situations like auto scaling.

##### INTERNET GATEWAY
1. Directing traffic to your VPC
2. Allow communication between instances in your VPC and the internet.
3. Are horizontally scaled, redundant and highly available by default.
4. Provide a target in your VPC route tables for internet- routable traffic.

What are the components of devops?
1. Continuous integration
2. Continuous testing
3. Continuous development
4. Continuous Feedback
5. Continuous monitoring
6. Continuous Deployment

##### What is codebuild in aws devops?
1. AWS codebuild compiles source code, run tests, and produes softwrare packages which are ready for deploy.
2. With codebuild, you can provision, manage and scale your own construct servers.

##### What is codeDeploy in aws devops?
1. CodeDeploy is an automated service which processes the deploying code to any instances either it is local servers or Amazon's EC2 instances
2. It mainly handles all the complexity that is involved in updating the applications for release.
3. The direct advantage of codeDeploy is it's functionality that helps users rapidly release new builds and model features and avoid any sort of downtime during this process of deployment.

##### What is codepipeline in aws devops?
1. Codepipeline is an aws service which provides continuous integration and continuous delivery.
2. After every single build, the operations such as building, testing and deployment becomes very easy with the set release model protocols that are defined by a user.
3. Codepipeline ensures delivering reliability for new software updates and features rapidly.

##### What is codestar in aws devops?
1. Aws codestar enables you to speedly produce, fabricate and deploy applications on AWS.
2. AWS codestar provides a unified fanatic interface, enabling you to easily govern your software to facilitate happenings in one area.
3. With AWS codestar you can set up your entire continuous delivery toolchain in minutes, allowing you to begin releasing code faster.

##### What is the difference between devops and agile?
A. A key principle of agile revolves around seamless development or production of software.
Devops deals with the deployment of the software. The main objective is to make sure there are faster turnaround time, high reliability and minimum error.

##### What is AWS lambda in AWS devops?
A. Lambda is a comutation service where the users can run their code without having to provision or manage servers explicitly.
With out prior integration, the users can run any piece of code for their applications or servers by using AWS lambda.
It is very simple as uploading a piece of code and lambda take cares of everything that is required to run and scale the code.

##### What is AWS code commit?
A. Code commit is a source control service provided in AWS that hosts git repositories safely in a highly scalable manner.
Using codecommit one can remove the set up and maintenance of a source control system requirement and scale it's infrastructure as per need.

##### What is meant by infrastructure as code(IaC)?
A. IaC is a common devops feature in which the code and the software development technique helps in managing the overall infrastructure from continuous integration to the version control system
Moreover, the API model in the cloud helps developers work on the entire infrastructure Programmatically.

##### What is VPC in AWS devops?
A VPC stands for virtual private cloud which is a cloud network that is mapped to an AWS account.
The AWS infrastructure helps users to create regions, subjects, routing tables and even internet gateways in the AWS accounts.
It provides users with the ability to use EC2 Or RDS as per requirements.

##### AWS cloud formation
AWS cloud formation is the services which provide simpler and easy access for businesses and developers.
It builds a collection of related AWS resources. Moreover, it enables businesses and developers to provide these elements in a predictable and orderly manner.

##### What is hybrid cloud in AWS devops?
A hybrid cloud is the computation setting which uses a combination of private and public clouds.
Hybrid clouds are created using a VPN tunnel that is inserted between the cloud VPN and the on-premises network.
AWS direct connect has the ability to bypass the internet and connect securely between the VPN and a data centre easily.

##### What is EBS?
EBS is a virtual storage area
Elastic Block Storage names the block-level volumes of storage, which are used in the EC2 machines.
It is highly compatible with other instances and stores the data reliabily.

##### What is a build project?
A build project defines how codebuild will run a build.
It includes the following information such as
1.where to get the source code?
2. Which build environment to use?
3. What build commands to run?
4. Where to store the build outpost?

##### What is AWS LoT?
AWS LoT is a cloud management platform that adds provisions for connected devices for interacting with all the cloud applications smoothly and securely.

##### what is the difference between security groups and NSCLs?
1. SG acts at instance level.
 NACLs acts at subnet level
2. SG allows to add or remove rules for both ingress and egress traffic to the instance.
A network ACLs is a layer of security for the VPC that acts as a firewall for controlling trarric in and out of one or more subnets.
3. SG comes with default allow all eg or ess and no ingress traffic.
Default ACL allows all inbound and outbound traffic . Newly created ACL denies all in and out traffic.

##### How many IAM keys can a user have ?
At a time user can have only 2 active IAM access and secret key 

##### How will you revoke the access keys?
Console> IAM Consoles>User>Security credentials>make inactive

##### How do you configure a public and private subnet in VPC?
1. Create VPC, Create 2 subnets, create internet gateway and assign IGW to the VPC.
2. To create a public subnet: Create a route table associate the subnet and a route using IGW ARN.
3. To create a private subnet: Create a NAT gateway, create a route table, associate the subnet and create a route using NAT ARN.

##### Route53 routing policy use cases
AWS Route53 is a fully managed DNS service, it allows to host/buy domain with AWS. Route53 support different routing policy to direct your traffic to your resources.
http://docs.aws.amazon.com/Route53/latest/DeveloperGuide/routing-policy.html

##### What are the types of pricing models for EC2 instances and what is the difference between them?
Reserved, Spot and On-demand instances.
https://aws.amazon.com/ec2/pricing/

##### How will you configure a password less access between two servers?
1. Create a key pair on both the servers using ssh-keygen -t rsa
2. Create a .ssh/authorized_keys on both the servers
3. Copy the key of server A on the above path of server B and vice versa
4. Give permission chmod -R 700 TO .ssh directory
5. Give permission chmod -R 600 to authorized _keys file

##### How will you configure password login on your AWS EC2 instance?
1. Create a user, assign a password to it.
2. Make entry in /etc/sudoers.d/
3. Edit /etc/sshdconfig file and uncomment password authentication Yes

##### You have two servers (A and B) in your AWS account and you have allowed ssh between both of them, but you are not able to ping each other. What could be issue and how do you resolved it?
ICMP protocol is not allowed between them allow the same in security group. In case ICMP are allowed in security groups. Then allow ICMP on the NACLs.

##### Where do you define your subnets while configuring autoscaling?
You neither define subnets while configuring launch nor while configuring auto-scaling. Subnets are defined while creating ELB.

##### How do you see and retreive the files from Glacier?
Amazon Glacier provides a management console, which you can use to create and delete vaults. However, you cannot download archives from archives from Amazon Glacier by using the management console. To download data, such as photos, videos and other documents, you must either use the AWS CLI or write code to make requests, by using either the REST API directly or by using the AWS SDKs.

##### What is Bastion host?
Bastion are like jump servers to allow access to the host in the private subnet.
The configurations usually work like below:
1. Bastion needs to configured to allow inbound ssh access (TCP -22) only from restricted ips (103.252.24.158/32, 32 here indicates exact IP address)

##### What is DR, its important aspect you consider while implementing DR strategy and what are the kind of DR strategies available?
This article covers almost almost everything.

##### What is a VPC subnet?
Ans:
A VPC is a virtual private cloud (VPC) that spans an AWS region. There are two or more availability zones in a region. Subnets within a VPC are used to logically isolate resources within a region. A subnet can't be shared by more than one availability region. Depending on its accessibility from outside of VPC and whether it can access resources outside of VPC, a subnet can be classified as either a private or public subnet.

In VPC, a subnet is an important part. A VPC may contain all public subnets (or a mix of public and private subnets). A subnet that does not have a route to the internet gateway is known as a private subnet. By routing traffic via a virtual private gateway, a subnet can be configured as a VPN-only subnet.

##### Where do you define subnets while configuring autoscaling?
You neither define subnets while configuring launch configuration nor while configuring auto-scaling. Subnets are defined while creating ELB

##### What is cloud computing?
Cloud computing refers to the on-demand delivery of IT resources and applications via the internet with pay as you go.
If we need we can get it right way. If we dont we can give it away.
Cloud computing is the practice of using a network of remote servers hosted on the Internet to store, manage and process data rather than a local server or a personal computer.
Features:
1. On demand provisioning
2. Scalability in minutes: We can scale out, or scale in
Cloud models:
Public
Private
Hybrid
Community

##### What are the default users to login to EC2?
EC2 user
ubuntu
centos
root
Admin

##### EC2 instances
When you launch an EC2 instance, you receive a public IP address by which that instance is reachable.
Once you stop that instance and restart you get a new public IP for the same instances.
So public IP gets changed every time for an instance after stop/start.
To overcome with this problem, we attach an elastic IP to an instance which doesnt change after you stop/start the instance as many times.

##### Advantages of having elastic IP
It is kind of static IP for your instance.
Doesn't change after stop/start.

##### EC2 load balancer
It is simply a software to manage the load of application.
For ex: If there are 10 web servers, we have public ip for one web servers, the traffic flow to one web server will be balanced across the 10 webservers using load balancer. To automatically distribute incoming web traffic across multiple ec2 instances. Add and remove ec2 instances without changing the inflow. If one of the instance fails, AWS ELB will automatically redirect the traffic to another running instance. If the failed instance is restored, ELB restores the traffic to that instance. ELB is like a traffic manager, it minimizes the risk of overloading one single instance and provides continuous monitoring of the health of AWS instances.


##### Elastic block storage
EBS is a virtual hard drive that can be used with ec2 instances.
Can attach it to ec2 instance.
Can place file system on it.
It appears as a mounted device.

##### S3-EBS-EFS
S3: Simple storage service can be used when the requirement is for WORM- write once and read many times.
EBS: Elastic block storage works better as server disks- high performance in terms of read and write.
EFS: Elastic file system is used to share the file system across different servers.


##### Relational database service:
1. RDS allows to create mysql, mariadb, oracle sql, postgresql, Aurora db(Amazon's database)
2. Easy administration
3. RDS db instances are preconfigured with appropriate settings.
MAIN BENEFITS
1. AWS takes care of underlying platform, so automatic updates
2. Automatic backups.
3. Automatic recovery in case of failures.

##### DNS/ROUTE 53: Domain Management Service
Key fatures:
1. Domain registration
2. DNS - Domain Name System Service
3. Health Checking

##### How Security Groups are configured?
A. 1. By default, all newly created security groups "allow all outbound traffic" to all destinations.
2. Modifying the default outbound rule on security groups increases complexity and is not recommended unless required for compliance.
3. Most organisations create security groups with "inbound rules" for "each functional tier"(web/app/data/etc) within an application.

##### What is IAM?
AWS Identity and Access Management (IAM) is a web service that helps you securely control access to AWS resources. You use IAM to control who is authenticated (signed in) and authorized (has permissions) to use resources.

##### What is S3
Simple Storage Service of AWS is abbreviated as S3 which is used to store and retrieve data anywhere at any time on the web. With S3, you can make online payments on the go and it is also defined to make the web-scale computing process simpler for the developers.

##### List out the main components in AWS
The beneficial components involved in AWS are as follows:
1. Simple Mail Service: Through this users can share emails via SMTP or restful API call.
2. Route 53: It is declared to a DNS web service.
3. Simple Storage Device S3: This is one of the leading storage devices used in AWS Identity and Access Management.
4. Elastic Compute Cloud EC2: It is referred to be an on-demand computing resource used for hosting applications. At the time of unforeseen workloads, EC2 is mainly used.
5. Elastic Block Store EBS: Here you can store constant data volumes that are being integrated with EC2 and that allows you to persist data.
6. Cloud Watch: It paves the way to examine all the components of AWS and you can also set an instant reminder to troubleshoot critical AWS instances.

##### Define the difference between an instance and AMI?
With a single AMI present, you can retrieve or download several instances as required. The hardware of any host computer can be defined by an instance. Every instance created has several features and functionalities with which you can accomplish computational and storage facilities. An instance is also declared to be a traditional host via which we can collaborate in the same manner as we interact with a computer.

##### What is known as EIP in AWS?
The Elastic IP Address (EIP) offered by AWS is a static ipv4 address which is used to manage and process dynamic cloud computing services. It is essential to connect an AWS account with the EIP because as and when you require an ipv4 static address for any of your instances, you can acquire it from the associated EIP which enables prompt communication with the active internet.

##### What is DevOps ?

DevOps is the combination of cultural philosophies, practices, and tools that increases an organization’s ability to deliver applications and services at high velocity: evolving and improving products at a faster pace than organizations using traditional software development and infrastructure management processes. This speed enables organizations to better serve their customers and compete more effectively in the market.

DevOps process can be visualized as an infinite loop, comprising these steps: plan, code, build, test, release, deploy, operate, monitor.



##### What is CodeBuild in AWS DevOps?
AWS provides CodeBuild, which is a fully managed in-house build service, thereby helping in the compilation of source code, testing, and the production of software packages that are ready to deploy. There is no need for management, allocation, or provision to scale the build servers as this is automatically scaled.

Build operations occur concurrently in servers, thereby providing the biggest advantage of not having to leave any builds waiting in a queue.

##### How can you handle continuous integration and deployment in AWS DevOps?
One must use AWS Developer tools to help get started with storing and versioning an application’s source code. This is followed by using the services to automatically build, test, and deploy the application to a local environment or to AWS instances.

It is advantageous, to start with the CodePipeline to build the continuous integration and deployment services and later on use CodeBuild and CodeDeploy as per need.


##### What is AWS Lambda in AWS DevOps?
AWS Lambda is a computation service that lets users run their code without having to provision or manage servers explicitly. Using AWS Lambda, the users can run any piece of code for their applications or services without prior integration. It is as simple as uploading a piece of code and letting Lambda take care of everything else required to run and scale the code.

##### Explain Amazon EC2 in brief.
Amazon EC2, or Elastic Compute Cloud as it is called, is a secure web service that strives to provide scalable computation power in the cloud. It is an integral part of AWS and is one of the most used cloud computation services out there, helping developers by making the process of Cloud Computing straightforward and easy.

##### DevOps and Cloud computing: What is the need?
Development and Operations are considered to be one single entity in the DevOps practice. This means that any form of Agile development, alongside Cloud Computing, will give it a straight-up advantage in scaling practices and creating strategies to bring about a change in business adaptability. If the cloud is considered to be a car, then DevOps would be its wheels

##### What is CodePipeline in AWS DevOps?
CodePipeline is a service offered by AWS to provide continuous integration and continuous delivery services. Alongside this, it has provisions of infrastructure updates as well. Operations such as building, testing, and deploying after every single build become very easy with the set release model protocols that are defined by a user. CodePipeline ensures that you can reliably deliver new software updates and features rapidly.

##### What is CodeBuild in AWS DevOps?
AWS provides CodeBuild, which is a fully managed in-house build service, thereby helping in the compilation of source code, testing, and the production of software packages that are ready to deploy. There is no need for management, allocation, or provision to scale the build servers as this is automatically scaled.

Build operations occur concurrently in servers, thereby providing the biggest advantage of not having to leave any builds waiting in a queue.

##### How can you handle continuous integration and deployment in AWS DevOps?
One must use AWS Developer tools to help get started with storing and versioning an application’s source code. This is followed by using the services to automatically build, test, and deploy the application to a local environment or to AWS instances.
It is advantageous to begin with the CodePipeline to build the continuous integration and deployment services and later on using CodeBuild and CodeDeploy as per need.


##### What is Amazon S3 in AWS DevOps?
Amazon S3 or Simple Storage Service is an object storage service that provides users with a simple and easy-to-use interface to store data and effectively retrieve it whenever and wherever needed.

##### What is the function of Amazon RDS in AWS Devops?
Amazon Relational Database Service (RDS) is a service that helps users in setting up a relational database in the AWS cloud architecture. RDS makes it easy to set up, maintain, and use the database online.