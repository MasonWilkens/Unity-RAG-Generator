namespace Game
{
    public class CombatBase {}
    public class Enemy : CombatBase
    {
        public int Damage = 10;
        private float nextAttackTime;
        /// <summary>Checks the attack cooldown before another strike.</summary>
        public bool CanAttack(float now)
        {
            return now >= nextAttackTime;
        }
        /// <summary>Apply damage to the enemy health.</summary>
        public int ApplyDamage(int health)
        {
            return health - Damage;
        }
        public void Tick(float now)
        {
            if (CanAttack(now)) nextAttackTime = now + 2;
        }
    }
}
