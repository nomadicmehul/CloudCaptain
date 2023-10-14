# Run Ansible Playbook target as Vagrant Machines

This guide explains how to run Ansible playbook target as Vagrant machines in local Linux system. Ansible is a configuration management and automation tool that allows you to automate tasks and configurations on remote servers, including virtual machines managed by Vagrant.

## Prerequisites

Before you begin, ensure that you have the following components installed:

1. **Vagrant**: Vagrant is used to create and manage virtual machines. You can download and install it from [the Vagrant-script](https://github.com/kuldipmori/scripts-for-software-install/blob/main/shell/vagrant-install.sh).

2. **VirtualBox**: Vagrant relies on a virtualization provider, such as VirtualBox. You can download and install VirtualBox from [the VirtualBox website](https://www.virtualbox.org/).

3. **Ansible**: Ansible is used to automate configurations on your Vagrant machines. Install Ansible on your local machine using your package manager (e.g., `apt`, `yum`, `brew`) or follow the instructions on the [the Ansible-script](https://github.com/kuldipmori/scripts-for-software-install/blob/main/shell/ansible-install.sh).


Here are the steps:

1. **Create Your Ansible Playbook**: Write or use an existing Ansible playbook (e.g., `apache.yaml`) that defines the tasks you want to perform on your Vagrant machine. Place this playbook file in your project directory.

2. **Configure Your Vagrantfile**: Ensure that your `Vagrantfile` is properly configured to set up your Vagrant machine. Specify the box, hostname, and any port forwarding you require. Also, set up the Ansible provisioning section to use the correct playbook file.

    ```ruby
    Vagrant.configure("2") do |config|
      config.vm.box = "ubuntu/trusty64"   # Specifies the base Ubuntu Vagrant image
      config.vm.hostname = "web"          # Sets the target name for the Ansible playbook

      # Port forwarding configuration
      config.vm.network "forwarded_port", guest: 80, host: 8080     # Forwards port 80 from the VM to port 8080 on your local machine

      # Ansible provisioning
      config.vm.provision "ansible_local" do |a|
        a.playbook = "apache.yaml"  # Specifies the Ansible playbook to run (replace with your playbook filename)
      end
    end
    ```

3. **Start Vagrant Machines and Run Ansible Playbook**: Open a terminal, navigate to your project directory containing the `Vagrantfile`, and run the following command:

    ```
    vagrant up
    ```

    This command creates and provisions your Vagrant machine(s) based on the configuration in your `Vagrantfile`. Ansible will automatically execute the specified playbook during the provisioning process.

4. **View the Results**: Once the `vagrant up` process is complete, you can access your Vagrant machine, which now has been configured according to your Ansible playbook. As per Vagrantfile port you can get output on http://localhost:8080

These steps allow you to efficiently manage your local Vagrant machine's configuration and provisioning using Ansible playbooks.
