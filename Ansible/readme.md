
Welcome to our comprehensive collection of learning resources for **Ansible**! Here, you'll discover a curated list of the best learning materials we've assembled just for you.

Take a moment to explore these valuable resources, handpicked to enhance your understanding of **Ansible**. We strive to provide the most up-to-date and informative content available.


# Ansible

- It is a tool designed with automation in mind from the start. Ansible focuses on providing “radically simple” configuration language as well as being able to manage cloud instances immediately with no modifications. It is also great for performing arbitrary IT orchestration (e.g., zero downtime rolling updates, hotfixes, etc.) as opposed to being configuration management specific. Rather than managing systems as individual units, you simply describe how components—and the system in general—interact with each other and Ansible will handle the rest.

- It allows you to organize your servers into groups, describe how those groups should be configured, and what actions should be taken on them, all from a central location.

## Introdution:
- What is Ansible?
  - Ansible allows you to achieve "Simple IT Automation". You describe the desired state of your machines, and Ansible manipulates them to achieve this state. The file in which you describe the desired state is called **Playbook**, which is written in **YAML**.

### Terminology:
- **Control Node**: the machine where Ansible is installed, responsible for running the provisioning on the servers you are managing.
- **Inventory**: an `INI` file that contains information about the servers you are managing.
- **Playbook**: a `YAML` file containing a series of procedures that should be automated.
- **Task**: a block that defines a single procedure to be executed, e.g.: install a package.
- **Module**: a module typically abstracts a system task, like dealing with packages or creating and changing files. Ansible has a multitude of built-in modules, but you can also create custom ones.
- **Role**: a set of related playbooks, templates and other files, organized in a pre-defined way to facilitate reuse and share.
- **Play**: a provisioning executed from start to finish is called a play.
- **Facts**: global variables containing information about the system, like network interfaces or operating system.
- **Handlers**: used to trigger service status changes, like restarting or reloading a service.

### Getting Started:
- A basic Ansible command or playbook:
  - selects machines to execute against from inventory
  - connects to those machines (or network devices, or other managed nodes), usually over SSH
  - copies one or more modules to the remote machines and starts execution there
- Working With Modules
  - Ansible ships with a number of modules (called the ‘module library’) that can be executed directly on remote hosts or through Playbooks.
  - Users can also write their own modules. These modules can control system resources, like services, packages, or files (anything really), or handle executing system commands.

### Resources:
- [Ansible Documentation](https://docs.ansible.com/)
- [Configuration Management 101: Writing Ansible Playbooks](https://www.digitalocean.com/community/tutorials/configuration-management-101-writing-ansible-playbooks)
- [Why you might need Ansible and not even know it](https://www.freecodecamp.org/news/why-you-might-need-ansible-and-not-even-know-it-d33b6e4b2ebe/)
- [Ansible Roles, Integration with Jenkins in DevOps, and EC2 Modules](https://www.softwaretestinghelp.com/ansible-roles-jenkins-integration-ec2-modules/)
- [Playing with Ansible... on Docker](https://mklein.io/2018/02/23/ansible-docker/)

If you have any additional resources or links that you believe would benefit others, please feel free to contribute. Our goal is to create a repository of the best learning materials, ensuring everyone has access to top-notch content.

We appreciate your visit to this repository. If you find our initiatives valuable, kindly star this repository to show your support.

Thank you once again, and happy learning!
