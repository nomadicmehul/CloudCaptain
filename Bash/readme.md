Welcome to our comprehensive collection of learning resources for **Bash**! Here, you'll discover a curated list of the best learning materials we've assembled just for you.

Take a moment to explore these valuable resources, handpicked to enhance your understanding of **Bash**. We strive to provide the most up-to-date and informative content available.

### What is Shell?

> Quoting from wikipedia

    A Unix shell is a command-line interpreter or shell that provides a traditional Unix-like command line user interface. Users direct the operation of the computer by entering commands as text for a command line interpreter to execute, or by creating text scripts of one or more such commands. Users typically interact with a Unix shell using a terminal emulator, however, direct operation via serial hardware connections, or networking session, are common for server systems. All Unix shells provide filename wildcarding, piping, here documents, command substitution, variables and control structures for condition-testing and iteration

- Interprets user commands
    - from terminal, from a file or as a shell script
    - expand wildcards, command/variable substitution
- Command history, command completion and command editing
- Managing processes
- Shell variables to customize the environment
- Difference between shell, tty and console


### Popular Shells

> Like any indispensible software, Shell has undergone transformation from the days of basic sh shell that was used in 1970s Unix. While bash is default shell in most distros and most commonly used, powerful and feature rich shells are still being developed and released

- sh bourne shell (light weight Linux distros might come with sh shell only)
- bash bourne again shell
- csh C shell
- tcsh tenex C shell
- ksh Korn shell
- zsh Z shell (bourne shell with improvements, including features from bash, tcsh, ksh)
- cat /etc/shells displays list of login shells available in the current Linux distro
- echo $SHELL path of current user's login shell

## Bash scripts - Examples

1. [Hello.sh](scripts/hello-world.sh): get a simple output
2. [Process.sh](scripts/process.sh): execute more than one command in a script
3. [Interactive.sh](scripts/interactive.sh): a simple but very much interactive script
4. [Special-Pattern.sh](scripts/special-pattern.sh): draw a diamond pattern with dots(.)
5. [While-Read.sh](scripts/while-read.sh): read lines from a file using while loop
6. [Read-Menu.sh](scripts/read-menu.sh): display a menu for system information
7. [While-Menu.sh](scripts/while-menu.sh): a repeated menu for system information
8. [Affect.sh](scripts/affect.sh): print a spinner loader
9. [Colorful.sh](scripts/color.sh): provide you with the output of several colours
10. [Convertlowercase.sh](scripts/convertlowercase.sh): convert data either from the file or standard input to lowercase
11. [up.sh](scripts/up.sh): move up a directory in shell script
12. [List-dir.sh](scripts/list-dir.sh): list files in a directory
13. [Count-lines.sh](scripts/count-lines.sh): print out the line number of each file in current directory
14. [Randomfile.sh](scripts/randomfile.sh): create unique file/folder automatically with date and time stamp
15. [Random-emoji.sh](scripts/random-emoji.sh): print ramdom emojis
16. [pomodoro.sh](scripts/pomodoro.sh): a simple pomodoro app written in bash

## Programming

1. [VersionCompare.sh](scripts/versioncompare.sh): compare two version number

## Utility

1. [Encrypt.sh](scripts/encrypt.sh): encrypt a file/folder with password
2. [Archive-and-encrypt.sh](scripts/archive-and-encrypt.sh): archive a path into a file and encrypt the file
3. [weather.sh](scripts/weather.sh): check the weather in a specified location or using the geolocation of the ip address by default.
4. [WhereIP.sh](scripts/whereIP.sh): Get location of an IP address. 

## System Administration

1. [DirectorySize.sh](scripts/directorysize.sh): output a specified directory's size
2. [Test-File.sh](scripts/test-file.sh): evaluate the status of a file/directory
3. [Server-Health.sh](scripts/server-health.sh): report server related information
4. [CPU.sh](scripts/cpu.sh): report if CPU usage exceeds the threshold
5. [Disk-Space.sh](scripts/disk-space.sh): check if the disk space crosses the limit
6. [CollectNetworkInfo.sh](scripts/collectnetworkinfo.sh): gather information related to server
7. [RemoteBackup.sh](scripts/remotebackup.sh): backup a local file into a remote server
8. [HardwareInfo.sh](scripts/hardware_machine.sh): show hardware information for systems Linux 
9. [Get-Temperature.sh](scripts/get-temperature.sh): show CPU temperature

## Math

1. [Addition.sh](scripts/addition.sh): perform addition of two numbers
2. [Subtraction.sh](scripts/subtraction.sh): perform subtraction of two numbers
3. [Multiplication.sh](scripts/multiplication.sh): perform multiplication of two numbers
4. [Division.sh](scripts/division.sh): perform division of two numbers
5. [Simplecacl.sh](scripts/simplecalc.sh): a simple calculator
6. [Table.sh](scripts/table.sh): print table of any number
7. [EvenOdd.sh](scripts/evenodd.sh): check if a number input from standard input is odd or even
8. [Factorial.sh](scripts/factorial.sh): generate the factorial of a number
9. [Armstrong.sh](scripts/armstrong.sh): check if a provided number is Armstrong or not
10. [Prime.sh](scripts/prime.sh): check if a number is prime or not
11. [Fibonacci.sh](scripts/fibonacci.sh): test if a number being entered is a Fibonacci or not
12. [Decimal2Binary.sh](scripts/decimal2binary.sh): convert Decimal Number to Binary
13. [Binary2Decimal.sh](scripts/binary2decimal.sh): convert Binary Number back to decimal
14. [Decimal2Hex.sh](scripts/dec2hex.sh): convert Decimal Number to Hex
15. [Hex2Decimal](scripts/hextodec.sh): convert Hex number back to Decimal

### Further Reading

- [Comparison of command shells](https://en.wikipedia.org/wiki/Comparison_of_command_shells)
- [Features and differences between various shells](http://www.faqs.org/faqs/unix-faq/shell/shell-differences/)
- [syntax comparison on different shells with examples](http://hyperpolyglot.org/unix-shells)
- [bash shell has also been ported on Windows platform]()
    - [git bash](https://git-for-windows.github.io/)
    - [Cygwin](https://www.cygwin.com/)
    - [MinGW](http://www.mingw.org/)
    - [Linux Subsystem for Windows](http://www.howtogeek.com/249966/how-to-install-and-use-the-linux-bash-shell-on-windows-10/)
- [Shell, choosing shell and changing default shells](https://wiki.ubuntu.com/ChangingShells)
- [shell scripting and important linux questions and answers](https://ashwikatech.blogspot.com/2021/12/shell-scripting.html?m=1)
- [shell-scripting-cheatsheet](./shell-scripting-cheatsheet.md)

 
## [Shell Customization](https://learnbyexample.gitbooks.io/linux-command-line/content/Shell_Customization.html)

- [Variables](https://learnbyexample.gitbooks.io/linux-command-line/content/Shell_Customization.html#variables)
- [Config files](https://learnbyexample.gitbooks.io/linux-command-line/content/Shell_Customization.html#config-files)
- [Emac mode Readline shortcuts](https://learnbyexample.gitbooks.io/linux-command-line/content/Shell_Customization.html#emac-mode-readline-shortcuts)

## Original Content and Authors

- [Shell - learnbyexample.gitbooks.io](https://learnbyexample.gitbooks.io/linux-command-line/content/Shell.html)

If you have any additional resources or links that you believe would benefit others, please feel free to contribute. Our goal is to create a repository of the best learning materials, ensuring everyone has access to top-notch content.

We appreciate your visit to this repository. If you find our initiatives valuable, kindly star this repository to show your support.

Thank you once again, and happy learning!