public class CountingDock {
 public static void Run(int shipment) {
  if (shipment > 0) CountingRelay.Transfer(shipment);
 }
}
