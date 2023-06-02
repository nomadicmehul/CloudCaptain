# OpenStack


Welcome to our comprehensive collection of learning resources for **OpenStack**! Here, you'll discover a curated list of the best learning materials we've assembled just for you.

Take a moment to explore these valuable resources, handpicked to enhance your understanding of **OpenStack**. We strive to provide the most up-to-date and informative content available.

## Checklist

- [ ] **Components & Services**
  - [ ] Neutron (Networking)
  - [ ] Cinder (Storage)
  - [ ] Nova (Compute)

- [ ] **Neutron**
  - [ ] Agents
  - [ ] Server

## Resources

Name | Description
:------|:------:
[OpenStack Operator Tools](https://github.com/openstack/osops-tools-contrib) | Tools and scripts for neutron, nova, etc.
[Infrared](https://github.com/redhat-openstack/infrared) | OpenStack Deployment with Ansible

## Cheatsheet

### Infrared

* Add plugin: `infrared plugin add <plugin_path>`
* Add plugin when the spec not in root directory: `infrared plugin add <plugin_path> --src-path infrared_plugin`
* Remove plugin: `infrared plugin remove <plugin_name>`

* Import workspace (= inventory update): `infrared workspace import https://my-jenkins.com/job/blipblop/1/artifact/workspace.tgz`
* List nodes in inventory/workspace: `infrared workspace node-list`

* If you get error when executing external plugin: "no such role" then you need to create a soft link: 
```
mkdir ~/.infrared/plugins/<plugin_name>/infrared_plugin/roles
ln -s ~/<plugin_name> ~/.infrared/plugins/<plugin_name>/infrared_plugin/roles/<plugin_name>
```

### Servers

* List openstack instances: `openstack server list`

If you have any additional resources or links that you believe would benefit others, please feel free to contribute. Our goal is to create a repository of the best learning materials, ensuring everyone has access to top-notch content.

We appreciate your visit to this repository. If you find our initiatives valuable, kindly star this repository to show your support.

Thank you once again, and happy learning!
