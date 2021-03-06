import inspect
import logging
import sys
import typing
from collections import defaultdict
from itertools import chain

from construct import Construct, Struct, BitStruct, Const, Nibble, Checksum, Padding, Bytes, this, RawCopy, Int8ub, \
    Default, Enum, Flag, BitsInteger, Int16ub, Container, EnumIntegerString, Rebuild

from paradox.config import config as cfg
from paradox.lib.utils import sanitize_key
from .common import calculate_checksum, ProductIdEnum, CommunicationSourceIDEnum, HexInt

logger = logging.getLogger('PAI').getChild(__name__)


class Panel:
    mem_map = {}
    event_map = {}
    property_map = {}

    def __init__(self, core, product_id, variable_message_length = True):
        self.core = core
        self.product_id = product_id
        self.variable_message_length = variable_message_length

    def parse_message(self, message, direction='topanel') -> typing.Optional[Container]:
        if message is None or len(message) == 0:
            return None

        if direction == 'topanel':
            if message[0] == 0x72 and message[1] == 0:
                return InitiateCommunication.parse(message)
            elif message[0] == 0x5F:
                return StartCommunication.parse(message)
        else:
            if message[0] == 0x72 and message[1] == 0xFF:
                return InitiateCommunicationResponse.parse(message)
            elif message[0] == 0x00 and message[4] > 0:
                return StartCommunicationResponse.parse(message)
            else:
                return None

    def get_message(self, name) -> Construct:
        clsmembers = dict(inspect.getmembers(sys.modules[__name__]))
        if name in clsmembers:
            return clsmembers[name]
        else:
            raise ResourceWarning('{} parser not found'.format(name))

    @staticmethod
    def get_error_message(error_code) -> str:
        # This is from EVO and may not apply to all panels

        error_str = str(error_code)
        if isinstance(error_code, EnumIntegerString):
            error_code = int(error_code)

        if error_code == 0x00:
            message = "Requested command did not work"
        elif error_code == 0x01:
            message = "User Code is invalid"
        elif error_code == 0x02:
            message = "Partition in code lockout (too many bad entries)"
        elif error_code == 0x05:
            message = "Panel will disconnect"
        elif error_code == 0x10:
            message = "Panel Not connected"
        elif error_code == 0x11:
            message = "Panel Already Connected"
        elif error_code == 0x12:
            message = "Invalid PC Password"
        elif error_code == 0x13:
            message = "Winload on phone line"
        elif error_code == 0x14:
            message = "Invalid Module address"
        elif error_code == 0x15:
            message = "Cannot write in RAM"
        elif error_code == 0x16:
            message = "Request to Upgrade Failed"
        elif error_code == 0x17:
            message = "Record number out of range"
        elif error_code == 0x19:
            message = "Invalid record type"
        elif error_code == 0x1A:
            message = "Multi-Bus not supported"
        elif error_code == 0x1B:
            message = "Incorrect number of users"
        elif error_code == 0x1C:
            message = "Invalid label number"
        else:
            message = error_str

        return message

    @staticmethod
    def encode_password(password) -> bytes:
        res = [0] * 2

        if password is None or password in [b'0000', '0000', 0]:
            return b'\x00\x00'

        if isinstance(password, int):
            password = str(password).zfill(4)

        if len(password) != 4:
            raise(Exception("Password length must be equal to 4. Got {}".format(len(password))))

        if not password.isdigit():
            raise (Exception("Not supported password {}".format(password)))

        int_password = int(password)
        i = min(4, len(password))
        i2 = i // 2 - 1

        while i > 0:
            b = int(int_password % 10)
            if b == 0:
                b = 0x0a

            int_password = int_password // 10

            if i % 2 == 0:
                res[i2] = b
            else:
                res[i2] = ((b << 4) | res[i2]) & 0xff
                i2 -= 1

            i -= 1

        return bytes(res)

    async def load_labels(self):
        logger.info("Updating Labels from Panel")

        data = defaultdict(dict)

        for elem_type in self.mem_map['elements']:
            elem_def = self.mem_map['elements'][elem_type]

            addresses = enumerate(chain.from_iterable(elem_def['addresses']), start=1)
            limits = cfg.LIMITS.get(elem_type)
            if limits is not None:
                addresses = [(i, a) for i, a in addresses if i in limits]

            await self._load_labels(data[elem_type], addresses, label_offset=elem_def['label_offset'])

            logger.info("{}: {}".format(elem_type.title(), ', '.join([v["label"] for v in data[elem_type].values()])))

        return data

    async def _load_labels(self,
                           data_dict: dict,
                           addresses: typing.List[typing.Tuple[int, int]],
                           field_length=16,
                           label_offset=0,
                           template=None):
        """
        Load labels from panel

        :param data_dict: Dict to fill
        :param addresses: Addresses list with indexes
        :param field_length: Text field length
        :param label_offset: Label offset
        :param template: Default template
        :return:
        """
        if template is None:
            template = {}

        for index, address in addresses:
            args = dict(address=address, length=field_length)
            reply = await self.core.send_wait(self.get_message('ReadEEPROM'), args, reply_expected=lambda m: m.fields.value.po.command == 0x05 and m.fields.value.address == address)

            if reply is None:
                logger.error("Could not fully load labels")
                return

            data = reply.fields.value.data
            b_label = data[label_offset:label_offset + field_length].strip(b'\0 ')

            label = b_label.replace(b'\0', b' ')

            try:
                label = label.decode(cfg.LABEL_ENCODING)
            except UnicodeDecodeError:
                logger.warning('Unable to properly decode label {} using the {} encoding.\n \
                    Specify a different encoding using the LABEL_ENCODING configuration option.'.format(b_label, cfg.LABEL_ENCODING))
                label = label.decode('utf-8', errors='ignore')

            properties = template.copy()
            properties['id'] = index
            properties['key'] = sanitize_key(label)
            properties['label'] = label
            data_dict[index] = properties

    def initialize_communication(self, reply, password):
        raise NotImplementedError("override initialize_communication in a subclass")

    def request_status(self, nr):
        raise NotImplementedError("override request_status in a subclass")

    def handle_status(self, message: Container, parser_map):
        """Handle MessageStatus"""
        mvars = message.fields.value

        if mvars.address not in parser_map:
            logger.error("Parser for memory address ({}) is not implemented. Please review your STATUS_REQUESTS setting. Skipping.".format(mvars.address))
            return

        parser = parser_map[mvars.address]
        try:
            return parser.parse(mvars.data)
        except Exception:
            logger.exception("Unable to parse RAM Status Block ({})".format(mvars.address))
            return

    def control_zones(self, zones, command) -> bool:
        raise NotImplementedError("override control_zones in a subclass")

    def control_partitions(self, partitions, command) -> bool:
        raise NotImplementedError("override control_partitions in a subclass")

    def control_outputs(self, outputs, command) -> bool:
        raise NotImplementedError("override control_outputs in a subclass")

    def dump_memory(self):
        raise NotImplementedError("override dump_memory in a subclass")

InitiateCommunication = Struct("fields" / RawCopy(
    Struct("po" / BitStruct(
        "command" / Const(7, Nibble),
        "reserved0" / Const(2, Nibble)),
        "reserved1" / Padding(35))),
    "checksum" / Checksum(Bytes(1), lambda data: calculate_checksum(data), this.fields.data))

InitiateCommunicationResponse = Struct("fields" / RawCopy(
    Struct(
        "po" / BitStruct(
            "command" / Const(7, Nibble),
            "message_center" / Nibble
        ),
        "new_protocol" / Const(0xFF, Int8ub),
        "protocol_id" / Int8ub,
        "protocol" / Struct(
            "version" / Int8ub,
            "revision" / Int8ub,
            "build" / Int8ub
        ),
        "family_id" / Int8ub,
        "product_id" / ProductIdEnum,
        "talker" / Enum(Int8ub,
                        BOOT_LOADER=0,
                        CONTROLLER_APPLICATION=1,
                        MODULE_APPLICATION=2),
        "application" / Struct(
            "version" / HexInt,
            "revision" / HexInt,
            "build" / HexInt),
        "serial_number" / Bytes(4),
        "hardware" / Struct(
            "version" / Int8ub,
            "revision" / Int8ub),
        "bootloader" / Struct(
            "version" / Int8ub,
            "revision" / Int8ub,
            "build" / Int8ub,
            "day" / Int8ub,
            "month" / Int8ub,
            "year" / Int8ub),
        "processor_id" / Int8ub,
        "encryption_id" / Int8ub,
        "reserved0" / Bytes(2),
        "label" / Bytes(8))),
    "checksum" / Checksum(Bytes(1), lambda data: calculate_checksum(data), this.fields.data))

StartCommunication = Struct("fields" / RawCopy(
    Struct(
        "po" / Struct("command" / Const(0x5F, Int8ub)),
        "validation" / Const(0x20, Int8ub),
        "_not_used0" / Padding(31),
        "source_id" / Default(CommunicationSourceIDEnum, 1),
        "user_id" / Struct(
            "high" / Default(Int8ub, 0),
            "low" / Default(Int8ub, 0)),
    )), "checksum" / Checksum(Bytes(1), lambda data: calculate_checksum(data), this.fields.data))

StartCommunicationResponse = Struct("fields" / RawCopy(
    Struct(
        "po" / BitStruct("command" / Const(0, Nibble),
                         "status" / Struct(
                             "reserved" / Flag,
                             "alarm_reporting_pending" / Flag,
                             "Winload_connected" / Flag,
                             "NeWare_connected" / Flag)
                         ),
        "_not_used0" / Bytes(3),
        "product_id" / ProductIdEnum,
        "firmware" / Struct(
            "version" / Int8ub,
            "revision" / Int8ub,
            "build" / Int8ub),
        "panel_id" / Int16ub,
        "_not_used1" / Bytes(5),
        "transceiver" / Struct(
            "firmware_build" / Int8ub,
            "family" / Int8ub,
            "firmware_version" / Int8ub,
            "firmware_revision" / Int8ub,
            "noise_floor_level" / Int8ub,
            "status" / BitStruct(
                "_not_used" / BitsInteger(6),
                "noise_floor_high" / Flag,
                "constant_carrier" / Flag,
            ),
            "hardware_revision" / Int8ub,
        ),
        "_not_used2" / Bytes(14),
    )),
    "checksum" / Checksum(Bytes(1), lambda data: calculate_checksum(data), this.fields.data))

CloseConnection = Struct("fields" / RawCopy(
    Struct(
        "po" / Struct(
            "command" / Const(0x70, Int8ub)
        ),
        "length" / Rebuild(Int8ub, lambda
            this: this._root._subcons.fields.sizeof() + this._root._subcons.checksum.sizeof()),
        "_not_used0" / Padding(34),
    )),
    "checksum" / Checksum(Bytes(1), lambda data: calculate_checksum(data), this.fields.data))

