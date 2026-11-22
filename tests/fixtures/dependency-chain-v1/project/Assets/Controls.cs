public class AmbiguousDock {
 public static void Run(int shipment) { if (shipment > 0) { ParcelRelay.Transfer(shipment); AlternateRelay.Transfer(shipment); } }
}
public static class AlternateRelay { public static void Transfer(int value) { Records.RecordBook.Append(value); } }
public class CyclicDock {
 public static void Run(int shipment) { if (shipment > 0) CycleRelay.Transfer(shipment); }
}
public static class CycleRelay { public static void Transfer(int value) { CyclicDock.Run(value); } }
public class DeepDock {
 public static void Run(int shipment) { if (shipment > 0) ExtraRelay.Transfer(shipment); }
}
public static class ExtraRelay { public static void Transfer(int value) { ParcelRelay.Transfer(value); } }
public class VirtualDock {
 public static void Run(int shipment) { if (shipment > 0) new VirtualRelay().Transfer(shipment); }
}
public class VirtualRelay { public virtual void Transfer(int value) { Records.RecordBook.Append(value); } }
public class VirtualLeafDock {
 public static void Run(int shipment) { if (shipment > 0) VirtualLeafRelay.Transfer(shipment); }
}
public static class VirtualLeafRelay { public static void Transfer(int value) { new VirtualBook().Append(value); } }
public class VirtualBook { private int auditStamp; public virtual void Append(int value) { auditStamp++; } }
public interface IBook { void Append(int value); }
public class InterfaceDock {
 public static void Run(int shipment, IBook book) { if (shipment > 0) InterfaceRelay.Transfer(shipment, book); }
}
public static class InterfaceRelay { public static void Transfer(int value, IBook book) { book.Append(value); } }
public class DelegateDock {
 public static void Run(int shipment) { if (shipment > 0) DelegateRelay.Transfer(shipment); }
}
public static class DelegateRelay {
 public static void Transfer(int value) { System.Action<int> write = Records.RecordBook.Append; write(value); }
}
public class StringDock {
 public static void Run(int shipment) { if (shipment > 0) StringRelay.Transfer(shipment); }
}
public static class StringRelay {
 public static void Transfer(int value) { Queue("Records.RecordBook.Append audit stamp", value); }
 private static void Queue(string label, int value) { }
}
public class MissingDock { public static void Run(int shipment) { MissingRelay.Transfer(shipment); } }
public class DirectDock { public static void Run(int shipment) { Records.RecordBook.Append(shipment); } }
public class RedundantDock {
 private static int auditStamp;
 public static void Run(int shipment) { auditStamp++; ParcelRelay.Transfer(shipment); }
}
public class DecoyDock { public static void Run(int shipment) { DecoyRelay.Transfer(shipment); } }
public static class DecoyRelay { public static void Transfer(int value) { DecoyBook.Append(value); } }
public static class DecoyBook {
 public static string Append(int payload) {
  // auditStamp is only an obsolete comment here.
  return "audit stamp";
 }
}
