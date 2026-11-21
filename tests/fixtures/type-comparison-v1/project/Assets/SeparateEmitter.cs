public class SeparateEmitter {
 private int charge, cooldown;
 public void Add(int elapsed) {
  charge += elapsed * 9;
 }
 public void Delay(int elapsed) {
  cooldown = elapsed * 8;
 }
}
