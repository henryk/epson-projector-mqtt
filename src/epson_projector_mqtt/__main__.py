import asyncio
import contextlib
import json
import logging
import platform
import sys
from asyncio import Condition
from contextlib import AsyncExitStack
from dataclasses import dataclass, asdict
from itertools import chain

from asyncio_mqtt import Client, MqttError, Will
from epson_projector import Projector
from epson_projector.const import PWR_OFF, PWR_ON
from everett.manager import ConfigManager

logger = logging.getLogger("epson_projector_mqtt.main_loop")


@dataclass
class ProjectorConfiguration:
    type: str
    timeout_scale: float

    @classmethod
    def from_config(cls, config):
        if config('type', default="serial") == "serial":
            return SerialProjectorConfiguration(
                type="serial",
                device=config("device"),
                timeout_scale=config("timeout_scale", default="2.0", parser=float),
            )
        else:
            return TCPProjectorConfiguration(
                type="tcp",
                host=config("host"),
                port=config("port", parser=int),
                timeout_scale=config("timeout_scale", default="2.0", parser=float),
            )

    def get_projector(self) -> Projector:
        return Projector(**asdict(self))

    def get_unique_id(self) -> str:
        return ":".join(
            chain(
                (platform.node(), self.type),
                (getattr(self, k) for k in ["device", "host", "port"] if hasattr(self, k))
            )
        )


@dataclass
class SerialProjectorConfiguration(ProjectorConfiguration):
    device: str

    def get_projector(self) -> Projector:
        return Projector(**{k.replace("device", "host"): v for (k, v) in asdict(self).items()})


@dataclass
class TCPProjectorConfiguration(ProjectorConfiguration):
    host: str
    port: int


class EpsonProjectorMqttMainLoop:
    def __init__(self):
        self.config = ConfigManager.basic_config()

        self.mqtt_config = self.config.with_namespace("mqtt")
        self.projector_config = self.config.with_namespace("projector")
        self.epson_projector_configuration = ProjectorConfiguration.from_config(self.projector_config)
        self.instance_name = self.config(
            "instance_name", default="epson_projector_" + platform.node()
        )
        self.name = self.config("name", default="Epson Projector")

        discovery_prefix = self.config("discovery_prefix", default="homeassistant")
        self.mqtt_prefix = "/".join(
            [
                discovery_prefix,
                "switch",
                self.instance_name,
            ]
        )

    def get_mqtt_client(self) -> Client:
        kwargs = {
            "hostname": self.mqtt_config("host", doc="MQTT host to connect to"),
            "port": self.mqtt_config(
                "port", default="1883", parser=int, doc="MQTT port to connect to"
            ),
            "will": Will(f"{self.mqtt_prefix}/status", "offline", retain=True),
        }
        username = self.mqtt_config("username", default="") or None
        password = self.mqtt_config("password", default="") or None

        if username and password:
            kwargs.update(username=username, password=password)

        return Client(**kwargs)

    async def send_discovery(self, client: Client):
        disc_config = {
            "~": self.mqtt_prefix,
            "state_topic": "~/state",
            "command_topic": "~/set",
            # 'expire_after': 10,
            "name": self.name,
            # 'unit_of_measurement': 'ppm',
            'icon': 'mdi:projector',
            "availability_topic": "~/status",
            "payload_available": "online",
            "payload_not_available": "offline",
            # 'json_attributes_topic': "{}/attributes".format(self.prefix),
            # 'force_update': self.config.get('force_update', False),
            "unique_id": self.epson_projector_configuration.get_unique_id(),
            "device": {
                # 'connections': [
                #    ['usb', self.config.get('device', '/dev/co2mini0')],
                # ],
                "identifiers": ("dns", platform.node()),
                "manufacturer": "Epson",
                "model": "EH-TW...",
            },
        }
        await client.publish(
            f"{self.mqtt_prefix}/config", json.dumps(disc_config), retain=True
        )

    async def main_loop(self):
        async with AsyncExitStack() as stack:
            tasks = set()
            stack.push_async_callback(cancel_tasks, tasks)

            client = self.get_mqtt_client()

            await stack.enter_async_context(client)
            logger.info("Connected to MQTT: %r", client)

            await self.send_discovery(client)
            projector = self.epson_projector_configuration.get_projector()

            async def close_projector():
                projector.close()

            stack.push_async_callback(close_projector)

            await client.publish(f"{self.mqtt_prefix}/status", "online", retain=True)

            wakeup = Condition()
            desired_state = [None]

            async def process_messages():
                async for message in messages:
                    payload = message.payload.decode()
                    if payload in ("ON", "OFF"):
                        desired_state[0] = payload
                        async with wakeup:
                            wakeup.notify_all()

            tasks.add(asyncio.create_task(process_messages()))

            manager = client.filtered_messages(f"{self.mqtt_prefix}/set")
            messages = await stack.enter_async_context(manager)

            async def handle_projector():
                last_power_state = None

                while True:
                    power_state = await projector.get_power()
                    if power_state is None:
                        raise EnvironmentError("Error querying projector status")

                    power_state = "ON" if power_state in ("01", "02", "03") else "OFF"
                    if power_state != last_power_state:
                        await client.publish(f"{self.mqtt_prefix}/state", power_state)
                        last_power_state = power_state

                    lamp_hours = await projector.get_property("LAMP")
                    source = await projector.get_property("SOURCE")
                    color_mode = await projector.get_property("CMODE")
                    mute_selection = await projector.get_property("MSEL")
                    luminance = await projector.get_property("LUMINANCE")

                    async with wakeup:
                        with contextlib.suppress(asyncio.TimeoutError):
                            await asyncio.wait_for(wakeup.wait(), 10)

                            if desired_state[0] != power_state:
                                await projector.send_command(PWR_ON if desired_state[0] == "ON" else PWR_OFF)

            tasks.add(asyncio.create_task(handle_projector()))

            await client.subscribe(f"{self.mqtt_prefix}/set")

            try:
                await asyncio.gather(*tasks)
            finally:
                await client.publish(f"{self.mqtt_prefix}/status", "offline", retain=True)


async def cancel_tasks(tasks):
    for task in tasks:
        if task.done():
            continue
        try:
            task.cancel()
            await task
        except asyncio.CancelledError:
            pass


async def main_inner():
    reconnect_interval = 3
    while True:
        try:
            m = EpsonProjectorMqttMainLoop()
            await m.main_loop()
        except MqttError as error:
            print(f'Error "{error}". Reconnecting in {reconnect_interval} seconds.')
        finally:
            await asyncio.sleep(reconnect_interval)


def main():
    logging.basicConfig(level=logging.DEBUG)
    logging.getLogger("everett").level = logging.INFO

    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    asyncio.run(main_inner())


if __name__ == "__main__":
    main()
