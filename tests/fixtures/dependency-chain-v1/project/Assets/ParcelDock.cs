public partial class ParcelDock {
 public static void Run(int shipment) {
  if (shipment > 0) ParcelRelay.Transfer(shipment);
 }
}
