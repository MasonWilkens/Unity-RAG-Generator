namespace Workshop {
 public class Stepper {
  private int charge, cooldown;
  public void Tick(int elapsed) {
   charge += elapsed * 3;
   cooldown = elapsed + 4;
  }
 }
}
namespace Runtime {
 public class Stepper {
  private int charge, cooldown;
  public void Tick(int elapsed) {
   charge -= elapsed * 5;
   cooldown = elapsed + 6;
  }
 }
}
