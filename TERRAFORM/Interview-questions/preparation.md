

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
