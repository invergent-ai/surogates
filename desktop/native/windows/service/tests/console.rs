// The guest's console on Windows itself: the service's end of COM1's pipe, as HCS's worker opens its other end, and
// what the worker writes there told as the console's text.

#![cfg(windows)]

use std::sync::atomic::{AtomicU32, Ordering};
use std::sync::mpsc::channel;
use std::sync::{Arc, Mutex};
use std::time::Duration;

use pipe::win::{Pipe, Way};
use service::hyper::{console, Notice, Notify};

// A pipe name of the test's own, each unique.
fn name() -> String {
    static NEXT: AtomicU32 = AtomicU32::new(0);
    format!(r"\\.\pipe\surogate-console-test-{}-{}", std::process::id(), NEXT.fetch_add(1, Ordering::SeqCst))
}

#[test]
fn takes_com1_s_other_end_opened_to_read_and_to_write_and_tells_what_it_writes() {
    let name = name();
    let (to, from) = channel();
    let to = Mutex::new(to);
    let notify: Notify = Arc::new(move |notice| {
        if let Notice::Console(text) = notice {
            let _ = to.lock().unwrap().send(text);
        }
    });
    let reading = console(&name, notify).expect("the console's pipe is made");
    // HCS's worker opens COM1's pipe as a serial port's other end: to read what the host types and to write what the
    // guest prints.
    let worker = Pipe::open(&name).expect("COM1's other end opens the console's pipe to read and to write");
    let printed = "[    0.000000] Linux version 6.12.48\r\nsurogate: the protected-names rule attached 16 of 16 hooks\r\n";
    worker.write_all(printed.as_bytes()).unwrap();
    let mut told = String::new();
    while told.len() < printed.len() {
        told.push_str(&from.recv_timeout(Duration::from_secs(10)).expect("the console's text is told"));
    }
    assert_eq!(told, printed);
    drop(worker);
    drop(reading);
}

#[test]
fn an_inbound_pipe_refuses_a_client_that_opens_it_to_read_and_to_write() {
    // Why the console's pipe is made both ways: a pipe its server alone reads refuses a client that asks to read it,
    // as COM1's other end does, so nothing the guest printed reached the console's log.
    let name = name();
    let _server = Pipe::server(&name, None, Way::Inbound, true, true).unwrap();
    assert!(Pipe::open(&name).is_err(), "an inbound pipe took a client that opens it to read");
}
