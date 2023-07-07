#Setting up Ansible Playbook for Windows Machine

Ansible can bring automation to a mixed operating system environment and provides an efficient way to get to an infrastructure-as-code (IaC) state without burdening your administrators.
Remoting into Windows servers or clients from the Ansible control machine requires Windows Remote Manager (WinRM) to be properly configured.

Run the following on Windows PowerShell:

```
$url = "https://raw.githubusercontent.com/ansible/ansible/devel/examples/scripts/ConfigureRemotingForAnsible.ps1"
$file = "$env:temp\ConfigureRemotingForAnsible.ps1"
(New-Object -TypeName System.Net.WebClient).DownloadFile($url, $file)
powershell.exe -ExecutionPolicy ByPass -File $file
winrm enumerate winrm/config/Listener
```

##Set up your inventory

```
[windows:vars]
ansible_user=Administrator@example.com
ansible_connection=winrm
ansible_port=5986
ansible_winrm_server_cert_validation=ignore
```

##Running the playbook

```ansible win-server -m win_ping```