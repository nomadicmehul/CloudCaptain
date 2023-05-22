## 💥"Terraform on Cloud"

##### What is "Terraform on Cloud"? 

Terraform is an open-source tool that allows you to define, provision, and manage infrastructure resources through a declarative syntax. 
           Terraform on Cloud extends the capabilities of Terraform by providing a cloud-native environment that enables seamless integration with various cloud providers, such as AWS, Azure, and Google Cloud.
           With Terraform on Cloud, you can automate the deployment and management of cloud resources in a scalable and repeatable manner. Terraform on Cloud provides features like remote state management, version control, and collaboration, which makes it easier for teams to work together on complex infrastructure projects.

##### How does "Terraform" on Cloud work? 

Terraform on Cloud works by leveraging the APIs provided by cloud providers to manage infrastructure resources. Terraform uses a declarative syntax called HashiCorp Configuration Language (HCL) to define the desired state of the infrastructure. 
             Terraform then compares the desired state with the current state of the infrastructure and applies the necessary changes to achieve the desired state.
Terraform on Cloud provides a cloud-native environment that simplifies the deployment and management of infrastructure resources. It integrates with various cloud providers, such as AWS, Azure, and Google Cloud, to provide a unified experience for managing cloud resources.


##### How to use Terraform on Cloud in production step by step? 

1. Define the infrastructure requirements in a Terraform script using HCL syntax.
2. Use Terraform on Cloud to apply the script to the target cloud environment.
3. Verify that the infrastructure is deployed correctly and the desired state is achieved.
4. Use Terraform on Cloud to manage the lifecycle of the infrastructure, such as updating or deleting resources.
5. Use Terraform on Cloud features, such as remote state management and version control, to ensure consistency and collaboration across teams.

##### How to learn "Terraform on Cloud" from scratch?

1. Learn the basics of infrastructure as code and Terraform.
2. Set up a cloud provider account, such as AWS or Azure, to practice with.
3. Read the Terraform documentation and follow the tutorials to understand the syntax and concepts of Terraform.
4. Practice writing Terraform scripts for different use cases, such as deploying virtual machines, storage, and networking resources.
5. Join the Terraform community forums and attend webinars to learn from experts and peers.
6. Experiment with Terraform on Cloud features, such as remote state management and version control.

##### What are the steps of Terraform’s core workflow?
A. The core workflow of Terraform has three steps.
1. The first is to write and includes creating infrastructure in code.
2. The second is to plan and involves planning ahead of time to check how the changes may look before implementing them.
3. Lastly, the third is to apply, which deals with creating a repeatable infrastructure.

##### What are some of the version control tools that Terraform supports?
Terraform supports version control tools like GitHub, GitLab CE, GitLab EE, and Bucket Cloud.

##### Define Modules in Terraform
A module in Terraform is a container for numerous resources that come in usage together. For example, we need the root module for every Terraform that involves resources referenced in the .tf files.

##### important terraform Commands

1.fmt
When we finish our terraform configuration we can make sure that everything is formatted correctly, run the fmt command first

2. init
After we use the format command, we have to initialize our working directory to prepare it for what we need. The init command looks at your configuration files and determines which providers and modules it needs to pull down from the registry to allow your configuration to work properly.

3. validate
Once we have initialized the directory it's good to run the validate command before you run plan or apply. Validation will catch syntax errors, version errors and other issues.

4.apply
This is the command that deploys or applies your configuration to a provider.

5.destroy
This command obviously will destroy your infrastructure.
