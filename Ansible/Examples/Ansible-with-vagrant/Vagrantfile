Vagrant.configure("2") do |config|
  config.vm.box = "ubuntu/trusty64"   # Specifies the base Ubuntu Vagrant image
  config.vm.hostname = "web"          # Sets the target name for the Ansible playbook

  # Port forwarding configuration
  config.vm.network "forwarded_port", guest: 80, host: 8080     # Forwards port 80 from the VM to port 8080 on your local machine
  
  # Ansible provisioning
  config.vm.provision "ansible_local" do |a|
    a.playbook = "apache.yaml"  # Specifies the Ansible playbook to run (replace with your playbook filename)
  end

    #Add more provisioning blocks for additional playbooks
    #  config.vm.provision "ansible_local" do |a|
    #    a.playbook = "playbook2.yaml"  # Specifies the second Ansible playbook to run
    #  end

end
