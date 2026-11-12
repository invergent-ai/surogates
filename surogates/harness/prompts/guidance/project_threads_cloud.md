---
name: project_threads_cloud
description: Injected for the master chat of a project in the cloud, after project_coordinator; says how it starts the project's threads.
applies_when: session.config.workstream_role is "coordinator" and the session names no device
---
## Starting threads

- Give it to a new thread with `agent`: the `description` is the thread's
  title, the `prompt` its goal. A thread works in the project's files in the
  cloud.
- This project works in the cloud. For work in a folder on the user's
  computer, tell the user to make a project in Surogate Desktop on that
  computer.
