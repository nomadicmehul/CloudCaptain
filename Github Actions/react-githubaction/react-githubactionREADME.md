# ReactJS GitHub Action

With the help of this GitHub Action file, you can automate the CI/CD process for React.js technology on any target server. You need to place this file in the repository path: `.github/workflows/*.yaml`

This GitHub Action has been thoroughly tested and is known to perform effectively in your use case, provided you follow the correct syntax, supply the required credentials for the deployment server, use the appropriate build command, and specify the correct Node.js version. Please make sure to refer to the README for guidance on securely adding your GitHub secrets.

## Overview

This GitHub Action workflow automatically builds and deploys a ReactJS application to a remote server using SFTP Action. The workflow is triggered on a `push` event to specific branches.

## Workflow Configuration

### Trigger

The workflow is triggered when a `push` event occurs on the following branches:

- `branch-1`

You can modify the list of branches to trigger the workflow by updating the `on` section in the workflow file.

```yaml
on:
  push:
    branches:
      - branch-1
```      

### Job: ubuntu-build

This job runs on an `ubuntu-latest` machine and uses a matrix strategy for the Node.js version. The workflow installs Node.js, npm dependencies, builds the code, and deploys it to the server.

```yaml
jobs:
  ubuntu-build:
    runs-on: ubuntu-latest
    strategy:
      matrix:
        node-version: [16.x]
```

### Secrets

Ensure that you have set the following secrets in your repository for secure configuration:

```yaml
USERNAME: The username for accessing the target server via SFTP. ( root or ubuntu )
HOST: The IP address or hostname of the target server. (IPv4 or Elastic IP)
KEY: The SSH private key file (in .pem or SSH format) used for server authentication.
```

### Must Follow

- Replace `branch-1` with your branch name.
- Adjust `node-version: [16.x]` to match your desired Node.js version.
- Modify `npm run build` as needed for your build command.
- Change `'./build/*'` to match the name of your artifacts folder.
- Adjust `'/var/www/project-name/build'` to specify the deployment path on your target server.
- Make sure to `maintain proper indentation` for the actions in the workflow.
- Ensure that the target server's `port 22` is open for GitHub Action artifact deployment.
- Set the necessary secrets in your GitHub repository for secure configuration.
- Set Secret in Repo `Settings` ->> `Secrets and Variables` ->> `Actions` ->> `New repository secret` (for add secret need admin access of github repo.).