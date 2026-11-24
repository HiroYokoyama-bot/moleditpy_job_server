# Limits

CPU and memory budgets are **advisory**: they decide when jobs start, not what a job may
use. The CPU budget counts physical cores, and a new server starts at this PC's core
count rather than its thread count: with hyper-threading a PC has more threads than cores,
and calculations rarely gain from the extra ones. On Linux only the CPUs the server may
run on count (`taskset`, containers, systemd limits). All jobs share the service OS
account, so they are not isolated from each other.
This is a service for trusted users. See [SECURITY.md](../SECURITY.md) and
[Execution and limits](EXECUTION.md).

**CPU in use now** is a share of every thread, so 100% means all threads are busy. When
the PC has more threads than cores, a dotted line on its trend ("Phys. cores") and a
dotted tick on its dial mark where every core is busy. The dial's colour follows the cores: green, amber from 70% of them, red from 90%
up to every core busy and a little beyond (10 points of the whole PC, since the OS uses some too), and purple past that, when jobs share cores with hyper-threads.
