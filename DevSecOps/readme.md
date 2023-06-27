Welcome to our comprehensive collection of learning resources for **DevSecOps**! Here, you'll discover a curated list of the best learning materials we've assembled just for you.

Take a moment to explore these valuable resources, handpicked to enhance your understanding of **DevSecOps**. We strive to provide the most up-to-date and informative content available.

<h1 align="center">
  <br>
  <a href=""><img src="https://user-images.githubusercontent.com/13212227/99404580-2374f000-292f-11eb-9348-284f24cca88c.png" alt="" width="500px;"></a>
  <br>
</h1>


<!-- omit in toc -->
## Contents
- [Resources](#resources)
  - [Books](./Books/)
  - [Articles](#articles)
  - [Books](#books)
  - [Communities](#communities)
  - [Conferences](#conferences)
  - [Newsletters](#newsletters)
  - [Podcasts](#podcasts)
  - [Secure Development Guidelines](#secure-development-guidelines)
  - [Secure Development Lifecycle Framework](#secure-development-lifecycle-framework)
  - [Toolchains](#toolchains)
  - [Training](#training)
  - [Wikis](#wikis)
- [Tools](#tools)
  - [Dependency Management](#dependency-management)
  - [Dynamic Analysis](#dynamic-analysis)
  - [Infrastructure as Code Analysis](#infrastructure-as-code-analysis)
  - [Intentionally Vulnerable Applications](#intentionally-vulnerable-applications)
  - [Monitoring](#monitoring)
  - [Secrets Management](#secrets-management)
  - [Secrets Scanning](#secrets-scanning)
  - [Static Analysis](#static-analysis)
  - [Supply Chain Security](#supply-chain-security)
  - [Threat Modelling](#threat-modelling)
- [Related Lists](#related-lists)

## Resources

### 0. DevSecOps Overview
  - Overview
    1. [DevSecOps in Wikipedia](https://en.wikipedia.org/wiki/DevOps#DevSecOps,_Shifting_Security_Left)
    2. [Zero to DevSecOps (OWASP Meetup)](https://owasp.org/www-chapter-belgium/assets/2019/2019-02-20/Zero-to-DevSecOps-OWASP-Meetup-02-19-19.pdf)
    3. [DevSecOps What Why And How (BlackHat USA-19)](https://i.blackhat.com/USA-19/Thursday/us-19-Shrivastava-DevSecOps-What-Why-And-How.pdf)
    4. [DevSecOps – Security and Test Automation (Mitre)](https://www.mitre.org/sites/default/files/publications/pr-19-0769-devsecops_security_test_automation-briefing.pdf)
    5. [DevSecOps: Making Security Central To Your DevOps Pipeline](https://spacelift.io/blog/what-is-devsecops)
    6. [Strengthen and Scale security using DevSecOps](https://owasp.org/www-pdf-archive/Devsecops-owasp-indonesia.pdf)
### 1. Design
  - Development Lifecycle
    1. [SDL(Secure Development Lifecycle) by Microsoft](https://www.microsoft.com/en-us/securityengineering/sdl/practices)
    2. [OWASP's Software Assurance Maturity Model](https://github.com/OWASP/samm)
    3. [Building Security In Maturity Model (BSIMM)](https://www.bsimm.com/framework.html)
    4. [NIST's Secure Software Developerment Framework](https://csrc.nist.gov/CSRC/media/Publications/white-paper/2019/06/07/mitigating-risk-of-software-vulnerabilities-with-ssdf/draft/documents/ssdf-for-mitigating-risk-of-software-vulns-draft.pdf)
    5. [DevSecOps basics: 9 tips for shifting left (Gitlab)](https://about.gitlab.com/blog/2020/06/23/efficient-devsecops-nine-tips-shift-left/)
    6. [6 Ways to bring security to the speed of DevOps (Gitlab)](https://about.gitlab.com/blog/2019/10/31/speed-security-devops/)
  - Threat Model
    1. [What is Threat Modeling / Wikipedia](https://en.wikipedia.org/wiki/Threat_model)
    2. [Threat Modeling by OWASP](https://owasp.org/www-community/Threat_Modeling)
    3. [Application Threat Modeling by OWASP](https://owasp.org/www-community/Application_Threat_Modeling)
    4. [Agile Threat Modeling Toolkit](https://threagile.io)
    5. [OWASP Threat Dragon](https://threatdragon.github.io)
### 2. Develop
  - Secure Coding
    1. [Secure coding guide by Apple](https://developer.apple.com/library/archive/documentation/Security/Conceptual/SecureCodingGuide/Introduction.html)
    2. [Secure Coding Guidelines for Java SE](https://www.oracle.com/java/technologies/javase/seccodeguide.html)
    3. [Go-SCP / Go programming language secure coding practices guide](https://github.com/OWASP/Go-SCP)
    4. [Android App security best practices by Google](https://developer.android.com/topic/security/best-practices)
    5. [Securing Rails Applications](https://guides.rubyonrails.org/security.html)
### 3. Build  
  - SAST(Static Application Security Testing)
    1. [Scan Source Code using Static Application Security Testing (SAST) with SonarQube, Part 1](https://medium.com/nycdev/scan-your-source-code-for-vulnerabilities-using-static-application-security-testing-sast-with-5f8ee1fdf9aa)
    2. [Announcing third-party code scanning tools: static analysis & developer security training](https://github.blog/2020-10-05-announcing-third-party-code-scanning-tools-static-analysis-and-developer-security-training/)
### 4. Test
  - DAST(Dynamic Application Security Testing)
    1. [Dynamic Application Security Testing with ZAP and GitHub Actions](https://www.zaproxy.org/blog/2020-05-15-dynamic-application-security-testing-with-zap-and-github-actions/) 
    2. [Dynamic Application Security Testing (DAST) in Gitlab](https://docs.gitlab.com/ee/user/application_security/dast/)
    3. [DAST using pdiscoveryio Nuclei (github action)](https://github.com/secopslab/nuclei-action)
    4. [ZAPCon 2021-Democratizing ZAP with test automation and domain specific languages](https://youtu.be/jimW-R6_F4U)
  - Penetration testing
    1. [Penetration Testing at DevSecOps Speed](https://securityboulevard.com/2019/04/penetration-testing-at-devsecops-speed/)
### 5. Deploy
  - Security Hardening & Config
    1. [CIS Benchmarks](https://www.cisecurity.org/cis-benchmarks/)
    2. [DevSecOps in Kubernetes](https://cloudblogs.microsoft.com/opensource/2019/07/22/devsecops-in-kubernetes/)
  - Security Scanning
    1. [Best practices for scanning images (docker)](https://docs.docker.com/develop/scan-images/)
### 6. Operate and Monitor
  - RASP(Run-time Application Security Protection)
    1. [Runtime Application Self-Protection by rapid7](https://www.rapid7.com/fundamentals/runtime-application-self-protection/)
    2. [Jumpstarting your devsecops - Pipeline with IAST and RASP](https://2018.appsec.eu/presos/DevOps_Jumpstarting-Your-DevSecOps_Jeff-Williams_AppSecEU2018.pdf)
  - Security Patch
    1. RASP(Runtime Application Self-Protection)
  - Security Audit
  - Security Monitor
    1. IAST(Interactive Application Security Testing)
    2. Metrics, Monitoring, Alerting
  - Security Analysis
    1. [Attack Surface Analysis Cheat Sheet by OWASP](https://cheatsheetseries.owasp.org/cheatsheets/Attack_Surface_Analysis_Cheat_Sheet.html)

## Security of CICD
- Github Actions
    1. [Security hardening for GitHub Actions](https://docs.github.com/en/actions/security-guides/security-hardening-for-github-actions)
    2. [Github Actions Security Best Practices](https://engineering.salesforce.com/github-actions-security-best-practices-b8f9df5c75f5)
    3. [GitHub Actions Security Best Practices [cheat sheet included]](https://blog.gitguardian.com/github-actions-security-cheat-sheet/)
- Jenkins
    1. [Securing Jenkins](https://www.jenkins.io/doc/book/security/)
    2. [Securing Jenkins CI Systems by SANS](https://www.sans.org/white-papers/36872/)
    3. [DEPRECATED/chef-jenkins-hardening](https://github.com/dev-sec/chef-jenkins-hardening)

### Articles

- [Our Approach to Employee Security Training](https://www.pagerduty.com/blog/security-training-at-pagerduty/) - _Pager Duty_ - Guidelines to running security training within an organisation.

### Books

- [Alice and Bob Learn Application Security](https://www.wiley.com/en-gb/Alice+and+Bob+Learn+Application+Security-p-9781119687405) - _Tanya Janca_ - An accessible and thorough resource for anyone seeking to incorporate, from the beginning of the System Development Life Cycle, best security practices in software development.

### Communities

- [MyDevSecOps](https://www.mydevsecops.io/) - _Snyk_ - A community that runs conferences, a blog, a podcast and a Slack workspace dedicated to DevSecOps.

### Conferences

- [AppSec Day](https://appsecday.io/) - _OWASP_ - An Australian application security conference run by OWASP.
- [DevSecCon](https://www.devseccon.com/) - _Snyk_ - A network of DevSecOps conferences run by Snyk.

### Newsletters
- [Shift Security Left](https://shift-security-left.curated.co/) - _Cossack Labs_ - A free biweekly newsletter for security-aware developers covering application security, secure architecture, DevSecOps, cryptography, incidents, etc. that can be useful for builders and (to a lesser extent) for breakers. 

### Podcasts

- [Absolute AppSec](https://absoluteappsec.com/) - _Seth Law & Ken Johnson_ - Discussions about current events and specific topics related to application security.
- [Application Security Podcast](https://podcast.securityjourney.com/) - _Security Journey_ - Interviews with industry experts about specific application security concepts.
- [BeerSecOps](https://blog.aquasec.com/devsecops-podcasts) - _Aqua Security_ - Breaking down the silos of Dev, Sec and Ops, discussing topics that span these subject areas.
- [DevSecOps Podcast Series](https://soundcloud.com/owasp-podcast) - _OWASP_ - Discussions with thought leaders and practitioners to integrate security into the development lifecycle.
- [The Secure Developer](https://www.mydevsecops.io/the-secure-developer-podcast) - _Snyk_ - Discussion about security tools and best practices for software developers.

### Secure Development Guidelines

- [Application Security Verification Standard](https://owasp.org/www-project-application-security-verification-standard/) - _OWASP_ - A framework of security requirements and controls to help developers design and develop secure web applications.
- [Coding Standards](https://wiki.sei.cmu.edu/confluence/display/seccode/SEI+CERT+Coding+Standards) - _CERT_ - A collection of secure development standards for C, C++, Java and Android development.
- [Fundamental Practices for Secure Software Development](https://safecode.org/wp-content/uploads/2018/03/SAFECode_Fundamental_Practices_for_Secure_Software_Development_March_2018.pdf) - _SAFECode_ - Guidelines for implementing key secure development practices throughout the SDLC.
- [Proactive Controls](https://owasp.org/www-project-proactive-controls/) - _OWASP_ - OWASP's list of top ten controls that should be implemented in every software development project.
- [Secure Coding Guidelines](https://wiki.mozilla.org/WebAppSec/Secure_Coding_Guidelines) - _Mozilla_ - A guideline containing specific secure development standards for secure web application development.
- [Secure Coding Practices Quick Reference Guide](https://owasp.org/www-pdf-archive/OWASP_SCP_Quick_Reference_Guide_v2.pdf) - _OWASP_ - A checklist to verify that secure development standards have been followed.

### Secure Development Lifecycle Framework

- [Building Security In Maturity Model (BSIMM)](https://www.bsimm.com/framework.html) - _Synopsys_ - A framework for software security created by observing and analysing data from leading software security initiatives.
- [Secure Development Lifecycle](https://www.microsoft.com/en-us/securityengineering/sdl/practices) - _Microsoft_ - A collection of tools and practices that serve as a framework for the secure development lifecycle.
- [Secure Software Development Framework](https://csrc.nist.gov/CSRC/media/Publications/white-paper/2019/06/07/mitigating-risk-of-software-vulnerabilities-with-ssdf/draft/documents/ssdf-for-mitigating-risk-of-software-vulns-draft.pdf) - _NIST_ - A framework consisting of practices, tasks and implementation examples for a secure development lifecycle.
- [Software Assurance Maturity Model](https://github.com/OWASP/samm) - _OWASP_ - A framework to measure and improve the maturity of the secure development lifecycle.

### Toolchains

- [Cloud Security and DevSecOps Best Practices _and_ Securing Web Application Technologies (SWAT) Checklist](https://www.sans.org/posters/cloud-security-devsecops-best-practices/) - _SANS_ - A poster containing the Securing Web Application Technologies (SWAT) Checklist, SANS Cloud Security Curriculum, Cloud Security Top 10, Top 12 Kubernetes Threats, and Secure DevOps Toolchain.
- [Periodic Table of DevOps Tools](https://xebialabs.com/periodic-table-of-devops-tools/) - _XebiaLabs_ - A collection of DevSecOps tooling categorised by tool functionality.

### Training

- [Application Security Education](https://github.com/duo-labs/appsec-education) - _Duo Security_ - Training materials created by the Duo application security team, including introductory and advanced training presentations and hands-on labs.
- [Cybrary](https://www.cybrary.it/) - _Cybrary_ - Subscription based online courses with dedicated categories for cybersecurity and DevSecOps.
- [PentesterLab](https://pentesterlab.com/) - _PentesterLab_ - Hands on labs to understand and exploit simple and advanced web vulnerabilities.
- [Practical DevSecOps](https://www.practical-devsecops.com) - _Practical DevSecOps_ - Learn DevSecOps concepts, tools, and techniques from industry experts with practical DevSecOps using state of the art browser-based labs.
- [SafeStack](https://academy.safestack.io/) - _SafeStack_ - Security training for software development teams, designed to be accessible to individuals and small teams as well as larger organisations.
- [Secure Code Warrior](https://www.securecodewarrior.com/) - _Secure Code Warrior_ - Gamified and hands-on secure development training with support for courses, assessments and tournaments.
- [SecureFlag](https://www.secureflag.com/platform.html) - _OWASP_ - Hands-on secure coding training for Developers and Build/Release Engineers.
- [Security Training for Engineers](https://sudo.pagerduty.com/for_engineers/) - _Pager Duty_ - A presentation created and open-sourced by PagerDuty to provide security training to software engineers.
- [Security Training for Everyone](https://sudo.pagerduty.com/for_everyone/) - _Pager Duty_ - A presentation created and open-sourced by PagerDuty to provide security training employees.
- [Web Security Academy](https://portswigger.net/web-security) - _PortSwigger_ - A set of materials and labs to learn and exploit common web vulnerabilities.
- [WeHackPuple](https://wehackpurple.com/) - _WeHackPurple_ - Online courses that teach application security theory and hands-on technical lessons.

### Wikis

- [DevSecOps Hub](https://snyk.io/devsecops/) - _Snyk_ - Introduction to key DevSecOps concepts, processes and technologies.
- [SecureFlag Knowledge Base](https://knowledge-base.secureflag.com/) - _OWASP_ - A repository of information about software vulnerabilities and how to prevent them.

## Tools

### Dependency Management

Open source software packages can speed up the development process by allowing developers to implement functionality without having to write all of the code. However, with the open source code comes open source vulnerabilities. Dependency management tools help manage vulnerabilities in open source packages by identifying and updating packages with known vulnerabilities.

- [Deepfence ThreatMapper](https://github.com/deepfence/ThreatMapper) - Apache v2, powerful runtime vulnerability scanner for kubernetes, virtual machines and serverless.
- [Dependabot](https://dependabot.com/) - _GitHub_ - Automatically scan GitHub repositories for vulnerabilities and create pull requests to merge in patched dependencies.
- [Dependency-Check](https://owasp.org/www-project-dependency-check/) - _OWASP_ - Scans dependencies for publicly disclosed vulnerabilities using CLI or build server plugins.
- [Dependency-Track](https://dependencytrack.org/) - _OWASP_ - Monitor the volume and severity of vulnerable dependencies across multiple projects over time.
- [JFrog XRay](https://jfrog.com/xray/) - _JFrog_ - Security and compliance analysis for artifacts stored in JFrog Artifactory.
- [NPM Audit](https://docs.npmjs.com/cli/audit) - _NPM_ - Vulnerable package auditing for node packages built into the npm CLI.
- [Renovate](https://renovate.whitesourcesoftware.com/) - _WhiteSource_ - Automatically monitor and update software dependencies for multiple frameworks and languages using a CLI or git repository apps.
- [Requires.io](https://requires.io/) - _Olivier Mansion & Alexis Tabary_ - Automated vulnerable dependency monitoring and upgrades for Python projects.
- [Snyk Open Source](https://snyk.io/) - _Snyk_ - Automated vulnerable dependency monitoring and upgrades using Snyk's dedicated vulnerability database.

### Dynamic Analysis

Dynamic Analysis Security Testing (DAST) is a form of black-box security testing where a security scanner interacts with a running instance of an application, emulating malicious activity to find common vulnerabilities. DAST tools are commonly used in the initial phases of a penetration test, and can find vulnerabilities such as cross-site scripting, SQL injection, cross-site request forgery and information disclosure.

- [Automatic API Attack Tool](https://github.com/imperva/automatic-api-attack-tool) - _Imperva_ - Perform automated security scanning against an API based on an API specification.
- [BurpSuite Enterprise Edition](https://portswigger.net/burp/enterprise) - _PortSwigger_ - BurpSuite's web application vulnerability scanner used widely by penetration testers, modified with CI/CD integration and continuous monitoring over multiple web applications.
- [Gauntlt](https://github.com/gauntlt/gauntlt) - _Gauntlt_ - A Behaviour Driven Development framework to run security scans using common security tools and test output, defined using Gherkin syntax.
- [Netz](https://github.com/spectralops/netz) - _Spectral_ - Discover internet-wide misconfigurations, using zgrab2 and others.
- [SSL Labs Scan](https://github.com/ssllabs/ssllabs-scan) - _SSL Labs_ - Automated scanning for SSL / TLS configuration issues.
- [Zed Attack Proxy (ZAP)](https://github.com/zaproxy/zaproxy) - _OWASP_ - An open-source web application vulnerability scanner, including an API for CI/CD integration.

### Infrastructure as Code Analysis

Infrastructure as Code allows applications to be deployed reliably to a consistent environment. This not only ensures that infrastructure is consistently hardened, but also provides an opportunity to statically and dynamically analyse infrastructure definitions for vulnerable dependencies, hard-coded secrets, insecure configuration and unintentional changes in security configuration. The following tools facilitate this analysis.

#### Multi-Platform

- [Checkov](https://github.com/bridgecrewio/checkov) - _Bridgecrew_ - Scan Terraform, AWS CloudFormation and Kubernetes templates for insecure configuration.
- [KICS](https://github.com/Checkmarx/kics) - _Checkmarx_ - Find security vulnerabilities, compliance issues, and infrastructure misconfigurations early in the development cycle.
- [Spectral DeepConfig](https://spectralops.io/blog/spectral-launches-deepconfig-to-ensure-no-misconfiguration-at-all-layers-of-software/) - _Spectral_ - Find misconfiguration both in infrastructure as well as apps as early as commit time.
- [Terrascan](https://github.com/accurics/terrascan) - _Accurics_ - Detect compliance and security violations across Infrastructure as Code to mitigate risk before provisioning cloud native infrastructure.

<!-- omit in toc -->
#### Cloud Formation
- [Cfn Nag](https://github.com/stelligent/cfn_nag) - _Stelligent_ - Scan AWS CloudFormation templates for insecure configuration.

<!-- omit in toc -->
#### Containers
- [Anchore Engine](https://anchore.com/opensource/) - _Anchore, Inc_ - Deep inspection of Docker images for CVEs and checking against custom policies. Engine behind their enterprise products that integrate against registries, orchestrators and CI/CD products.
- [Clair](https://github.com/quay/clair) - _Quay_ - Scan App Container and Docker containers for publicly disclosed vulnerabilities.
- [Dagda](https://github.com/eliasgranderubio/dagda/) - _Elías Grande_ - Compares OS and software dependency versions installed in Docker containers with public vulnerability databases, and also performs virus scanning.
- [Docker-Bench-Security](https://github.com/docker/docker-bench-security) - _Docker_ - The Docker Bench for Security is a script that checks for dozens of common best-practices around deploying Docker containers in production.
- [Hadolint](https://github.com/hadolint/hadolint) - _Hadolint_ - Checks a Dockerfile against known rules and validates inline bash code in RUN statements.
- [Snyk Container](https://snyk.io/product/container-vulnerability-management/) - _Snyk_ - Scan Docker and Kubernetes applications for security vulnerabilities during CI/CD or via continuous monitoring.
- [Trivy](https://github.com/aquasecurity/trivy) - _Aqua Security_ - Simple and comprehensive vulnerability scanner for containers.

<!-- omit in toc -->
#### Terraform
- [Regula](https://github.com/fugue/regula) - _Fugue_ - Evaluate Terraform infrastructure-as-code for potential security misconfigurations and compliance violations prior to deployment.
- [Terraform Compliance](https://terraform-compliance.com/) - _terraform-compliance_ - A lightweight, security and compliance focused test framework against terraform to enable negative testing capability for your infrastructure-as-code.
- [Tfsec](https://github.com/liamg/tfsec) - _Liam Galvin_ - Scan Terraform templates for security misconfiguration and noncompliance with AWS, Azure and GCP security best practice.

<!-- omit in toc -->
#### Kubernetes
- [Kube-Score](https://github.com/zegl/kube-score) - _Gustav Westling_ - Scan Kubernetes object definitions for security and performance misconfiguration.
- [Kubectrl Kubesec](https://github.com/controlplaneio/kubectl-kubesec) - _ControlPlane_ - Plugin for kubesec.io to perform security risk analysis for Kubernetes resources.

#### Ansible
- [Ansible-Lint](https://github.com/ansible-community/ansible-lint) - _Ansible Community_ - Checks playbooks for practices and behaviour that could potentially be improved. As a community backed project ansible-lint supports only the last two major versions of Ansible. 

### Intentionally Vulnerable Applications

Intentionally vulnerable applications are often useful when developing security tests and tooling to provide a place you can run tests and make sure they fail correctly. These applications can also be useful for understanding how common vulnerabilities are introduced into applications and let you practice your skills at exploiting them.

- [Bad SSL](https://github.com/chromium/badssl.com) - _The Chromium Project_ - A container running a number of webservers with poor SSL / TLS configuration. Useful for testing tooling.
- [Cfngoat](https://github.com/bridgecrewio/cfngoat) - _Bridgecrew_ - Cloud Formation templates for creating stacks of intentionally insecure services in AWS. Ideal for testing the Cloud Formation Infrastructure as Code Analysis tools above.
- [Damn Vulnerable Web App](http://www.dvwa.co.uk/) - _Ryan Dewhurst_ - A web application that provides a safe environment to understand and exploit common web vulnerabilities.
- [Juice Shop](https://github.com/bkimminich/juice-shop) - _OWASP_ - A web application containing the OWASP Top 10 security vulnerabilities and more.
- [NodeGoat](https://github.com/OWASP/NodeGoat) - _OWASP_ - A Node.js web application that demonstrates and provides ways to address common security vulnerabilities.
- [Terragoat](https://github.com/bridgecrewio/terragoat) - _Bridgecrew_ - Terraform templates for creating stacks of intentionally insecure services in AWS, Azure and GCP. Ideal for testing the Terraform Infrastructure as Code Analysis tools above.
- [Vulnerable Web Apps Directory](https://owasp.org/www-project-vulnerable-web-applications-directory) - _OWASP_ - A collection of vulnerable web applications for learning purposes.

### Monitoring
It's not enough to test and harden our software in the lead up to a release. We must also monitor our production software for usage, performance and errors to capture malicious behavior and potential security flaws that we may need to respond to or address. A wide variety of tools are available to monitor different aspects of production software and infrastructure.

- [Csper](https://csper.io/report-uri) - _Csper_ - A set of Content Security Policy tools that can test policies, monitor CSP reports and provide metrics and alerts.

### Secrets Management

The software we write needs to use secrets (passwords, API keys, certificates, database connection strings) to access resources, yet we cannot store secrets within the codebase as this leaves them vulnerable to compromise. Secret management tools provide a means to securely store, access and manage secrets.

- [Ansible Vault](https://docs.ansible.com/ansible/latest/user_guide/vault.html) - _Ansible_ - Securely store secrets within Ansible pipelines.
- [AWS Key Management Service (KMS)](https://aws.amazon.com/kms/) - _Amazon AWS_ - Create and manage cryptographic keys in AWS.
- [AWS Secrets Manager](https://aws.amazon.com/secrets-manager/) - _Amazon AWS_ - Securely store retrievable application secrets in AWS.
- [Azure Key Vault](https://azure.microsoft.com/en-au/services/key-vault/) - _Microsoft Azure_ - Securely store secrets within Azure.
- [BlackBox](https://github.com/StackExchange/blackbox) - _StackExchange_ - Encrypt credentials within your code repository.
- [Chef Vault](https://github.com/chef/chef-vault) - _Chef_ - Securely store secrets within Chef.
- [CredStash](https://github.com/fugue/credstash) - _Fugue_ - Securely store secrets within AWS using KMS and DynamoDB.
- [CyberArk Application Access Manager](https://www.cyberark.com/products/privileged-account-security-solution/application-access-manager/) - _CyberArk_ - Secrets management for applications including secret rotation and auditing.
- [Docker Secrets](https://docs.docker.com/engine/swarm/secrets/) - _Docker_ - Store and manage access to secrets within a Docker swarm.
- [Git Secrets](https://github.com/awslabs/git-secrets) - _Amazon AWS_ - Scan git repositories for secrets committed within code or commit messages.
- [Gopass](https://github.com/gopasspw/gopass) - _Gopass_ - Password manager for teams relying on Git and gpg. Manages secrets in encrypted files and repositories.
- [Google Cloud Key Management Service (KMS)](https://cloud.google.com/kms) - _Google Cloud Platform_ - Securely store secrets within GCP.
- [HashiCorp Vault](https://www.vaultproject.io/) - _HashiCorp_ - Securely store secrets via UI, CLI or HTTP API.
- [Keyscope](https://github.com/SpectralOps/keyscope) - _Spectral_ - Keyscope is an open source key and secret workflow tool (validation, invalidation, etc.) built in Rust.
- [Pinterest Knox](https://github.com/pinterest/knox) - _Pinterest_ - Securely store, rotate and audit secrets.
- [Secrets Operations (SOPS)](https://github.com/mozilla/sops) - _Mozilla_ - Encrypt keys stored within YAML, JSON, ENV, INI and BINARY files.
- [Teller](https://github.com/spectralops/teller) - _Spectral_ - A secrets management tool for developers - never leave your command line for secrets.


### Secrets Scanning

Source control is not a secure place to store secrets such as credentials, API keys or tokens, even if the repo is private. Secrets scanning tools can scan and monitor git repositories and pull-requests for secrets, and can be used to prevent secrets from being committed, or to find and remove secrets that have already been committed to source control.

- [CredScan](https://secdevtools.azurewebsites.net/helpcredscan.html) - _Microsoft_ - A credential scanning tool that can be run as a task in Azure DevOps pipelines.
- [Detect Secrets](https://github.com/Yelp/detect-secrets) - _Yelp_ - An aptly named module for (surprise, surprise) detecting secrets within a code base.
- [GitGuardian](https://www.gitguardian.com/) - _GitGuardian_ - A web-based solution that scans and monitors public and private git repositories for secrets.
- [Gitleaks](https://github.com/zricethezav/gitleaks) - _Zachary Rice_ - Gitleaks is a SAST tool for detecting hardcoded secrets like passwords, api keys, and tokens in git repositories.
- [git-secrets](https://github.com/awslabs/git-secrets) - _AWS Labs_ - Scans commits, commit messages and merges for secrets. Native support for AWS secret patterns, but can be configured to support other patterns.
- [Nightfall](https://nightfall.ai/solutions/product/github) - _Nightfall_ - A web-based platform that monitors for sensitive data disclosure across several SDLC tools, including GitHub repositories.
- [Repo-supervisor](https://github.com/auth0/repo-supervisor) - _Auth0_ - Secrets scanning tool that can run as a CLI, as a Docker container or in AWS Lambda.
- [SpectralOps](https://spectralops.io) - _Spectral_ - Automated code security, secrets, tokens and sensitive data scanning.
- [truffleHog](https://github.com/trufflesecurity/truffleHog) - _Truffle Security_ - Searches through git repositories for secrets, digging deep into commit history and branches.

### Static Analysis

Static Analysis Security Testing (SAST) tools scan software for vulnerabilities without executing the target software. Typically, static analysis will scan the source code for security flaws such as the use of unsafe functions, hard-coded secrets and configuration issues. SAST tools often come in the form of IDE plugins and CLIs that can be integrated into CI/CD pipelines.

<!-- omit in toc -->
#### Multi-Language Support

- [DevSkim](https://github.com/microsoft/DevSkim) - _Microsoft_ - A set of IDE plugins, CLIs and other tools that provide security analysis for a number of programming languages.
- [Graudit](https://github.com/wireghoul/graudit/) - _Eldar Marcussen_ - Grep source code for potential security flaws with custom or pre-configured regex signatures.
- [Hawkeye](https://github.com/hawkeyesec/scanner-cli) - _Hawkeyesec_ - Modularised CLI tool for project security, vulnerability and general risk highlighting.
- [LGTM](https://lgtm.com/) - _Semmle_ - Scan and monitor code for security vulnerabilities using custom or built-in CodeQL queries.
- [RIPS](https://www.ripstech.com/) - _RIPS Technologies_ - Automated static analysis for PHP, Java and Node.js projects.
- [SemGrep](https://semgrep.dev/) - _r2c_ - Semgrep is a fast, open-source, static analysis tool that finds bugs and enforces code standards at editor, commit, and CI time.
- [SonarLint](https://www.sonarlint.org/) - _SonarSource_ - An IDE plugin that highlights potential security security issues, code quality issues and bugs. 
- [SonarQube](https://www.sonarqube.org/) - _SonarSource_ - Scan code for security and quality issues with support for a wide variety of languages.

<!-- omit in toc -->
#### C / C++

- [FlawFinder](https://github.com/david-a-wheeler/flawfinder) - _David Wheeler_ - Scan C / C++ code for potential security weaknesses.

<!-- omit in toc -->
#### C#

- [Puma Scan](https://github.com/pumasecurity/puma-scan) - _Puma Security_ - A Visual Studio plugin to scan .NET projects for potential security flaws.

<!-- omit in toc -->
#### Configuration Files
- [Conftest](https://github.com/instrumenta/conftest) - _Instrumenta_ - Create custom tests to scan any configuration file for security flaws.

<!-- omit in toc -->
#### Java

- [Deep Dive](https://discotek.ca/deepdive.xhtml) - _Discotek.ca_ - Static analysis for JVM deployment units including Ear, War, Jar and APK.
- [Find Security Bugs](https://github.com/find-sec-bugs/find-sec-bugs/) - _OWASP_ - SpotBugs plugin for security audits of Java web applications. Supports Eclipse, IntelliJ, Android Studio and SonarQube.
- [SpotBugs](https://github.com/spotbugs/spotbugs) - _SpotBugs_ - Static code analysis for Java applications.

<!-- omit in toc -->
#### JavaScript

- [ESLint](https://eslint.org/) - _JS Foundation_ - Linting tool for JavaScript with multiple security linting rules available.

<!-- omit in toc -->
#### Go

- [Golang Security Checker](https://github.com/securego/gosec) - _securego_ - CLI tool to scan Go code for potential security flaws.

<!-- omit in toc -->
#### .NET

- [Security Code Scan](https://github.com/security-code-scan/security-code-scan) - _Security Code Scan_ - Static code analysis for C# and VB.NET applications.

<!-- omit in toc -->
#### PHP

- [Phan](https://github.com/phan/phan) - _Phan_ - Broad static analysis for PHP applications with some support for security scanning features.
- [PHPCS Security Audit](https://github.com/FloeDesignTechnologies/phpcs-security-audit) - _Floe_ - PHP static analysis with rules for PHP, Drupal 7 and PHP related CVEs.
- [Progpilot](https://github.com/designsecurity/progpilot) - _Design Security_ - Static analysis for PHP source code.

<!-- omit in toc -->
#### Python

- [Bandit](https://github.com/PyCQA/bandit) - _Python Code Quality Authority_ - Find common security vulnerabilities in Python code.

<!-- omit in toc -->
#### Ruby

- [Brakeman](https://github.com/presidentbeef/brakeman) - _Justin Collins_ - Static analysis tool which checks Ruby on Rails applications for security vulnerabilities.
- [DawnScanner](https://github.com/thesp0nge/dawnscanner) - _Paolo Perego_ - Security scanning for Ruby scripts and web application. Supports Ruby on Rails, Sinatra and Padrino frameworks.


### Supply Chain Security

Supply chain attacks come in different forms, targeting parts of the SDLC that are inherently 3rd party: tools in CI, external code that's been executed, and more. Supply chain security tooling can defend against these kinds of attacks.

- [Harden Runner GitHub Action](https://github.com/step-security/harden-runner) - _StepSecurity_ - installs a security agent on the GitHub-hosted runner (Ubuntu VM) to prevent exfiltration of credentials, detect compromised dependencies and build tools, and detect tampering of source code during the build.
- [Preflight](https://github.com/spectralops/preflight) - _Spectral_ - helps you verify scripts and executables to mitigate supply chain attacks in your CI and other systems, such as in the recent [Codecov hack](https://spectralops.io/blog/credentials-risk-supply-chain-lessons-from-the-codecov-breach/).
- [Sigstore](https://www.sigstore.dev/) - sigstore is a set of free to use and open source tools, including [fulcio](https://github.com/sigstore/fulcio), [cosign](https://github.com/sigstore/cosign) and [rekor](https://github.com/sigstore/rekor), handling digital signing, verification and checks for provenance needed to make it safer to distribute and use open source software.

### Threat Modelling

Threat modelling is an engineering exercise that aims to identify threats, vulnerabilities and attack vectors that represent a risk to something of value.  Based on this understanding of threats, we can design, implement and validate security controls to mitigate threats. The following list of tools assist the threat modelling process.

- [Awesome Threat Modelling](https://github.com/hysnsec/awesome-threat-modelling) - _Practical DevSecOps_ - A curated list of threat modelling resources.
- [SecuriCAD](https://www.foreseeti.com/) - _Forseeti_ - Treat modelling and attack simulations for IT infrastructure.
- [IriusRisk](https://iriusrisk.com/) - _IriusRisk_ - Draw threat models and capture threats and countermeasures and manage risk.
- [Raindance Project](https://github.com/devsecops/raindance) - _DevSecOps_ - Use attack maps to identify attack surface and adversary strategies that may lead to compromise.
- [SD Elements](https://www.securitycompass.com/sdelements/threat-modeling/) - _Security Compass_ - Identify and rank threats, generate actionable tasks and track related tickets.
- [Threat Dragon](https://owasp.org/www-project-threat-dragon/) - _OWASP_ - Threat model diagramming tool.
- [Threat Modelling Tool](https://www.microsoft.com/en-us/securityengineering/sdl/threatmodeling) - _Microsoft_ - Threat model diagramming tool.
- [Threatspec](https://threatspec.org/) - _Threatspec_ - Define threat modelling as code.

## Related Lists

- [Awesome Dynamic Analysis](https://github.com/analysis-tools-dev/dynamic-analysis/) - _Matthias Endler_ - A collection of dynamic analysis tools and code quality checkers.
- [Awesome Static Analysis](https://github.com/analysis-tools-dev/static-analysis/) - _Matthias Endler_ - A collection of static analysis tools and code quality checkers.
- [Awesome Threat Modelling](https://github.com/hysnsec/awesome-threat-modelling) - _Practical DevSecOps_ - A curated list of threat modeling resources.
- [Vulnerable Web Apps Directory](https://owasp.org/www-project-vulnerable-web-applications-directory) - _OWASP_ - A collection of vulnerable web applications for learning purposes.

## Why collect the tools?
Spending a lot of time on applying DevSecOps is searching, comparing, and making decisions about tools. These tool lists are a good way to help you reduce unnecessary time and apply them quickly 😎

## List of Tool
| Type | Name | Description | Popularity | Language |
| ---------- | :---------- | :----------: | :----------: | :----------: | 
| Build/SAST  | [SonarQube](https://www.sonarqube.org/) |  SonarQube is an open-source platform for continuous inspection of code quality to perform automatic reviews with static analysis of code to detect bugs, code smells, and security vulnerabilities on 20+ programming languages.|![](https://img.shields.io/static/v1?label=&message=it's%20not%20github&color=gray)|![](https://img.shields.io/static/v1?label=&message=it's%20not%20github&color=gray)
| Build/SAST | [codeql](https://github.com/github/codeql) | CodeQL | ![](https://img.shields.io/github/stars/github/codeql) | ![](https://img.shields.io/github/languages/top/github/codeql) |
| Build/SAST | [ggshield](https://github.com/GitGuardian/ggshield) | An open source CLI from GitGuardian to detect 350+ types of hardcoded secrets and 70+ IaC misconfigurations | ![](https://img.shields.io/github/stars/GitGuardian/ggshield) | ![](https://img.shields.io/github/languages/top/GitGuardian/ggshield) |
| Build/SAST | [semgrep](https://github.com/returntocorp/semgrep) | Lightweight static analysis for many languages. Find bug variants with patterns that look like source code. | ![](https://img.shields.io/github/stars/returntocorp/semgrep) | ![](https://img.shields.io/github/languages/top/returntocorp/semgrep) |
| Build/SAST | [sonarcloud-github-action](https://github.com/SonarSource/sonarcloud-github-action) | Integrate SonarCloud code analysis to GitHub Actions | ![](https://img.shields.io/github/stars/SonarSource/sonarcloud-github-action) | ![](https://img.shields.io/github/languages/top/SonarSource/sonarcloud-github-action) |
| Build/SECRET-MANAGE | [kamus](https://github.com/Soluto/kamus) | An open source, git-ops, zero-trust secret encryption and decryption solution for Kubernetes applications | ![](https://img.shields.io/github/stars/Soluto/kamus) | ![](https://img.shields.io/github/languages/top/Soluto/kamus) |
| Build/SECRET-MANAGE | [secrets-sync-action](https://github.com/google/secrets-sync-action) | A Github Action that can sync secrets from one repository to many others. | ![](https://img.shields.io/github/stars/google/secrets-sync-action) | ![](https://img.shields.io/github/languages/top/google/secrets-sync-action) |
| Build/SECRET-MANAGE | [vault-action](https://github.com/hashicorp/vault-action) | A GitHub Action that simplifies using HashiCorp Vault ™ secrets as build variables. | ![](https://img.shields.io/github/stars/hashicorp/vault-action) | ![](https://img.shields.io/github/languages/top/hashicorp/vault-action) |
| Design/THREAT | [owasp-threat-dragon-desktop](https://github.com/mike-goodwin/owasp-threat-dragon-desktop) | An installable desktop variant of OWASP Threat Dragon | ![](https://img.shields.io/github/stars/mike-goodwin/owasp-threat-dragon-desktop) | ![](https://img.shields.io/github/languages/top/mike-goodwin/owasp-threat-dragon-desktop) |
| Design/THREAT | [pytm](https://github.com/izar/pytm) | A Pythonic framework for threat modeling | ![](https://img.shields.io/github/stars/izar/pytm) | ![](https://img.shields.io/github/languages/top/izar/pytm) |
| Design/THREAT | [seasponge](https://github.com/mozilla/seasponge) | SeaSponge is an accessible threat modelling tool from Mozilla | ![](https://img.shields.io/github/stars/mozilla/seasponge) | ![](https://img.shields.io/github/languages/top/mozilla/seasponge) |
| Design/THREAT | [threagile](https://github.com/Threagile/threagile) | Agile Threat Modeling Toolkit | ![](https://img.shields.io/github/stars/Threagile/threagile) | ![](https://img.shields.io/github/languages/top/Threagile/threagile) |
| Operate and Monitor/COMPONENT-ANALYSIS | [dependency-track](https://github.com/DependencyTrack/dependency-track) | Dependency-Track is an intelligent Component Analysis platform that allows organizations to identify and reduce risk in the software supply chain. | ![](https://img.shields.io/github/stars/DependencyTrack/dependency-track) | ![](https://img.shields.io/github/languages/top/DependencyTrack/dependency-track) |
| Operate and Monitor/K8S | [kube-hunter](https://github.com/aquasecurity/kube-hunter) | Hunt for security weaknesses in Kubernetes clusters | ![](https://img.shields.io/github/stars/aquasecurity/kube-hunter) | ![](https://img.shields.io/github/languages/top/aquasecurity/kube-hunter) |
| Test/DAST | [action-baseline](https://github.com/zaproxy/action-baseline) | A GitHub Action for running the OWASP ZAP Baseline scan | ![](https://img.shields.io/github/stars/zaproxy/action-baseline) | ![](https://img.shields.io/github/languages/top/zaproxy/action-baseline) |
| Test/DAST | [action-dalfox](https://github.com/hahwul/action-dalfox) | XSS scanning with Dalfox on Github-action | ![](https://img.shields.io/github/stars/hahwul/action-dalfox) | ![](https://img.shields.io/github/languages/top/hahwul/action-dalfox) |
| Test/DAST | [action-full-scan](https://github.com/zaproxy/action-full-scan) | A GitHub Action for running the OWASP ZAP Full scan | ![](https://img.shields.io/github/stars/zaproxy/action-full-scan) | ![](https://img.shields.io/github/languages/top/zaproxy/action-full-scan) |
| Test/DAST | [zaproxy](https://github.com/zaproxy/zaproxy) | The OWASP ZAP core project | ![](https://img.shields.io/github/stars/zaproxy/zaproxy) | ![](https://img.shields.io/github/languages/top/zaproxy/zaproxy) |
| Test/PENTEST | [faraday](https://github.com/infobyte/faraday) | Collaborative Penetration Test and Vulnerability Management Platform | ![](https://img.shields.io/github/stars/infobyte/faraday) | ![](https://img.shields.io/github/languages/top/infobyte/faraday) |
| Test/PENTEST | [metasploit-framework](https://github.com/rapid7/metasploit-framework) | Metasploit Framework | ![](https://img.shields.io/github/stars/rapid7/metasploit-framework) | ![](https://img.shields.io/github/languages/top/rapid7/metasploit-framework) |
| Test/PENTEST | [monkey](https://github.com/guardicore/monkey) | Infection Monkey - An automated pentest tool | ![](https://img.shields.io/github/stars/guardicore/monkey) | ![](https://img.shields.io/github/languages/top/guardicore/monkey) |
| Test/PENTEST | [nuclei](https://github.com/projectdiscovery/nuclei) | Fast and customizable vulnerability scanner based on simple YAML based DSL. | ![](https://img.shields.io/github/stars/projectdiscovery/nuclei) | ![](https://img.shields.io/github/languages/top/projectdiscovery/nuclei) |
| Test/PENTEST | [ptf](https://github.com/trustedsec/ptf) | The Penetration Testers Framework (PTF) is a way for modular support for up-to-date tools. | ![](https://img.shields.io/github/stars/trustedsec/ptf) | ![](https://img.shields.io/github/languages/top/trustedsec/ptf) |

If you have any additional resources or links that you believe would benefit others, please feel free to contribute. Our goal is to create a repository of the best learning materials, ensuring everyone has access to top-notch content.

We appreciate your visit to this repository. If you find our initiatives valuable, kindly star this repository to show your support.

Thank you once again, and happy learning!
 
