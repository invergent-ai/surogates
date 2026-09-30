def ok: .code == 0; def bad: .code != 0;
.asks as $asks |
.results | to_entries[] | .key as $var | .value.checks as $c |
( ["A","B","C"][] as $r | $c[$r] as $x |
  { var: $var, root: $r,
    ownWrite: ($x.ownWrite | ok and (.stdout|test("hi"))),
    hiddenPrivate: (($x.privateDoc|bad) and ($x.sshSecret|bad)),
    otherRootHidden: (($x.otherRead|bad) and ($x.otherWrite|bad)),
    tmpdir: ($x.tmpdirWrite|ok),
    tools: ($x.tools | ok and (.stdout|test("v22")) and (.stdout|test("git version"))),
    allowedNet: ($x.allowedNet.stdout == "200"),
    askNet: (if $r == "A" then $x.askNet.stdout == "200" else $x.askNet.stdout != "200" end),
    npm: ($x.npm|ok), pip: ($x.pip|ok),
    docker: ($x.dockerSock.stdout|test("no-docker-socket")),
    bus: ($x.sessionBus.stdout + $x.sessionBus.stderr | test("Failed|not permitted|No such")),
    asksForRoot: ([$asks[] | select(.variant==$var and .root==$r)] | length) } ),
{ var: $var, crossTmpIsolated: (($c.crossTmp.AreadsB|bad) and ($c.crossTmp.BreadsA|bad)) }
