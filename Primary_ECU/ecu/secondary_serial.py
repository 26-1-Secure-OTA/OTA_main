import os
import time
import serial


class SecondarySerial:
    def __init__(self, port=None, baud=115200, timeout=1):
        self.port = port or os.environ.get("STM32_PORT", "/dev/ttyACM0")
        self.baud = baud
        self.timeout = timeout

        self.ser = serial.Serial(self.port, self.baud, timeout=self.timeout)
        time.sleep(2)

        # STM32 READY 메시지가 있으면 비우기
        self.read_all(0.5)

    def read_all(self, wait=0.3) -> str:
        time.sleep(wait)
        data = self.ser.read_all()
        if not data:
            return ""
        return data.decode(errors="ignore").strip()

    def send_line(self, line: str, wait=0.1) -> str:
        if not line.endswith("\n"):
            line += "\n"
        self.ser.write(line.encode("utf-8"))
        return self.read_all(wait)

    def send_config(self, cfg_text: str) -> str:
        responses = []

        responses.append(self.send_line("BEGIN_CONFIG", wait=0.2))

        for line in cfg_text.splitlines():
            line = line.strip()
            if not line:
                continue
            resp = self.send_line(line, wait=0.05)
            if resp:
                responses.append(resp)

        responses.append(self.send_line("END_CONFIG", wait=0.7))

        return "\n".join([r for r in responses if r])

    def get_status(self) -> str:
        return self.send_line("GET_STATUS", wait=0.5)

    def close(self):
        self.ser.close()