auto-scaling-update.yaml - This CloudFormation template creates a new version of the launch template to install the CodeDeploy agent. It also updates the Auto Scaling group to use the new version of the launch template.

Issue the following AWS CLI command to update the Auto Scaling stack, replacing parameter values as necessary:

aws cloudformation update-stack --stack-name auto-scaling-production --template-url https://s3.amazonaws.com/architecting-operational-excellence-aws/auto-scaling-update.yaml --parameters ParameterKey=VPCStackName,ParameterValue=vpc-production ParameterKey=KeyName,ParameterValue=your_keypair --capabilities CAPABILITY_NAMED_IAM