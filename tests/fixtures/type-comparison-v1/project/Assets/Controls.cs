public class PlainEmitter {
 private int charge, cooldown;
 public string Describe() { return "charge cooldown"; }
}
public class TiedEmitter {
 private int charge, cooldown;
 public void Tick(int value) { charge += value; cooldown = value; }
 public void Tick(float value) { charge += (int)value; cooldown = (int)value; }
}
public class ThirdEmitter { private int charge; public void Tick() { charge++; } }
public abstract class AbstractEmitter { public abstract void Tick(int charge, int cooldown); }
