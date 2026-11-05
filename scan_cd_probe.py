import paramiko
ssh=paramiko.SSHClient(); ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
ssh.connect("192.168.0.2", username="abba", password="6926", timeout=30)
def r(c, t=90):
    _,o,_=ssh.exec_command(c, timeout=t); return o.read().decode(errors="replace")
out=r('"C:\\Program Files\\FizzDisk\\FizzDisk.exe" scan D: 2>&1')
print(out)
ssh.close()
