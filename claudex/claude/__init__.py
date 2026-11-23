"""Claude Desktop: chat records, the control socket client, launching and notices."""

# Listed in dependency order: lifecycle uses the other two.
from . import chats as chats, control as control, lifecycle as lifecycle
